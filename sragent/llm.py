"""Provider-agnostic LLM client with caching, cost tracking and a hard budget.

All providers are reached through the OpenAI-compatible Chat Completions API:
  * openai : api.openai.com
  * gemini : Google's OpenAI-compatible endpoint
  * mock   : deterministic in-process stub for offline tests

Every call is cached on disk (SQLite) keyed by (provider, model, messages, params),
so re-running an experiment never pays twice.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .config import load_secrets, role_cfg

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


class BudgetExceeded(RuntimeError):
    pass


class CostTracker:
    """Shared across roles. Logs every call to usage.jsonl and enforces budget."""

    def __init__(self, budget_usd: float, log_path: str | Path | None = None):
        self.budget = float(budget_usd) if budget_usd is not None else float("inf")
        self.spent = 0.0
        self.spent_equiv = 0.0   # what calls would have cost without the cache (for fair per-task costs)
        self.calls = 0
        self.cached_calls = 0
        self.lock = threading.Lock()
        self.log_path = Path(log_path) if log_path else None
        self.by_tag: dict[str, dict[str, float]] = {}
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # the budget is cumulative per run directory (survives restarts)
            if self.log_path.exists():
                for line in self.log_path.read_text().splitlines():
                    try:
                        self.spent += float(json.loads(line).get("usd", 0))
                    except ValueError:
                        pass
        self.spent_at_start = self.spent

    def check(self):
        if self.spent >= self.budget:
            raise BudgetExceeded(f"Budget ${self.budget:.2f} exhausted (spent ${self.spent:.4f}).")

    def add(self, role: str, model: str, tag: str, pin: int, pout: int, cost: float,
            seconds: float, cached: bool):
        with self.lock:
            self.spent_equiv += cost
            if cached:
                self.cached_calls += 1
            else:
                self.calls += 1
                self.spent += cost
            t = self.by_tag.setdefault(tag or "untagged", {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "sec": 0.0})
            t["calls"] += 1
            t["in"] += pin
            t["out"] += pout
            t["usd"] += 0.0 if cached else cost
            t["usd_equiv"] = t.get("usd_equiv", 0.0) + cost
            t["sec"] += seconds
            if self.log_path:
                with open(self.log_path, "a") as f:
                    f.write(json.dumps({"ts": time.time(), "role": role, "model": model, "tag": tag,
                                        "prompt_tokens": pin, "completion_tokens": pout,
                                        "usd": 0.0 if cached else round(cost, 6),
                                        "seconds": round(seconds, 2), "cached": cached}) + "\n")

    def summary(self) -> dict:
        return {"spent_usd": round(self.spent, 4), "spent_this_session_usd": round(self.spent - self.spent_at_start, 4),
                "budget_usd": self.budget,
                "api_calls": self.calls, "cache_hits": self.cached_calls,
                "by_tag": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                           for k, v in self.by_tag.items()}}


class DiskCache:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.Lock()
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT)")

    def get(self, k: str):
        with self.lock, sqlite3.connect(self.path) as c:
            row = c.execute("SELECT v FROM cache WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, k: str, v: Any):
        with self.lock, sqlite3.connect(self.path) as c:
            c.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (k, json.dumps(v)))


# ------------------------------------------------------------------ JSON helpers

_THINK = re.compile(r"<(think|thought)>.*?</(think|thought)>", re.S)


def parse_json(text: str) -> Any:
    """Robustly parse a JSON object from model output (handles <think>, fences, prose)."""
    if text is None:
        raise ValueError("empty response")
    t = _THINK.sub("", text).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # first balanced {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = t.find(open_c)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                ch = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == open_c:
                    depth += 1
                elif ch == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = t.find(open_c, start + 1)
    raise ValueError(f"could not parse JSON from: {t[:300]}")


# ------------------------------------------------------------------ client

class LLM:
    def __init__(self, cfg: dict, role: str, tracker: CostTracker, cache: DiskCache | None,
                 mock_fn: Callable[[list[dict], str], str] | None = None):
        self.role = role
        self.c = role_cfg(cfg, role)
        self.provider = self.c["provider"]
        if self.provider not in {"openai", "gemini", "mock"}:
            raise ValueError(f"Unsupported API provider: {self.provider}")
        self.model = self.c["model"]
        self.tracker = tracker
        self.cache = cache
        self.mock_fn = mock_fn
        self.client = None
        if self.provider != "mock":
            from openai import OpenAI
            secrets = load_secrets(self.c.get("api_env"), cfg.get("_config_dir"))
            if self.provider == "openai":
                key, base = secrets.get("openai_api_key"), self.c.get("base_url")
            elif self.provider == "gemini":
                key, base = secrets.get("gemini_api_key"), self.c.get("base_url") or GEMINI_BASE
            else:
                raise ValueError(f"unknown provider {self.provider}")
            if not key:
                raise RuntimeError(f"No API key for provider '{self.provider}'. Check api_env path "
                                   f"({self.c.get('api_env')}) or environment variables.")
            self.client = OpenAI(api_key=key, base_url=base, timeout=self.c["timeout"], max_retries=0)

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    def _cost(self, pin: int, pout: int) -> float:
        p = self.c.get("pricing") or {}
        return pin / 1e6 * float(p.get("input_per_m", 0)) + pout / 1e6 * float(p.get("output_per_m", 0))

    def chat(self, messages: list[dict], *, json_out: bool = True, tag: str = "",
             max_tokens: int | None = None, temperature: float | None = None,
             use_cache: bool = True) -> Any:
        temperature = self.c["temperature"] if temperature is None else temperature
        max_tokens = max_tokens or self.c["max_tokens"]
        key_src = json.dumps([self.provider, self.model, messages, json_out, temperature,
                              self.c.get("reasoning_effort"), self.c.get("extra_body")], sort_keys=True)
        key = hashlib.sha256(key_src.encode()).hexdigest()
        if use_cache and self.cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.tracker.add(self.role, self.model, tag, hit.get("pin", 0), hit.get("pout", 0),
                                 self._cost(hit.get("pin", 0), hit.get("pout", 0)), 0.0, True)
                return parse_json(hit["text"]) if json_out else hit["text"]

        self.tracker.check()
        msgs = list(messages)
        last_err = None
        for attempt in range(self.c["max_retries"] + 1):
            t0 = time.time()
            try:
                text, pin, pout = self._call(msgs, json_out, max_tokens, temperature)
                dt = time.time() - t0
                self.tracker.add(self.role, self.model, tag, pin, pout, self._cost(pin, pout), dt, False)
                if json_out:
                    try:
                        val = parse_json(text)
                    except ValueError as e:
                        last_err = e
                        msgs = messages + [{"role": "assistant", "content": text[:4000]},
                                           {"role": "user", "content": "Your reply was not valid JSON. "
                                            "Return ONLY the JSON object, nothing else."}]
                        continue
                else:
                    val = text
                if use_cache and self.cache:
                    self.cache.set(key, {"text": text, "pin": pin, "pout": pout})
                return val
            except BudgetExceeded:
                raise
            except Exception as e:  # network / rate limit / server errors
                last_err = e
                if any(k in str(e) for k in ("insufficient_quota", "credit_balance", "invalid_api_key", "PERMISSION_DENIED")):
                    raise RuntimeError(f"{self.name}: account/key problem, not retrying: {e}") from e
                if "context" in str(e).lower() and "length" in str(e).lower():
                    raise
                time.sleep(min(60, 2 ** attempt * 2))
        raise RuntimeError(f"LLM call failed after retries ({self.name}, tag={tag}): {last_err}")

    def _call(self, messages, json_out, max_tokens, temperature):
        if self.provider == "mock":
            text = self.mock_fn(messages, "json" if json_out else "text")
            return text, sum(len(m["content"]) // 4 for m in messages), len(text) // 4
        kw: dict[str, Any] = {"model": self.model, "messages": messages}
        reasoning = self.c.get("reasoning_effort")
        if self.provider == "openai" and reasoning:
            kw["reasoning_effort"] = reasoning
            kw["max_completion_tokens"] = max_tokens
        elif self.provider == "openai":
            kw["max_completion_tokens"] = max_tokens
            kw["temperature"] = temperature
        else:
            kw["max_tokens"] = max_tokens
            kw["temperature"] = temperature
            if self.provider == "gemini" and reasoning:
                kw["reasoning_effort"] = reasoning  # Gemini OpenAI-compat: low | medium | high
        if json_out and self.c.get("json_mode", True):
            kw["response_format"] = {"type": "json_object"}
        if self.c.get("extra_body"):
            kw["extra_body"] = self.c["extra_body"]
        try:
            r = self.client.chat.completions.create(**kw)
        except Exception as e:
            # Some reasoning models reject temperature; retry once without it.
            if "temperature" in str(e) and "temperature" in kw:
                kw.pop("temperature")
                r = self.client.chat.completions.create(**kw)
            else:
                raise
        text = r.choices[0].message.content or ""
        u = r.usage
        pin = getattr(u, "prompt_tokens", 0) or 0
        total = getattr(u, "total_tokens", 0) or 0
        pout = max(getattr(u, "completion_tokens", 0) or 0, total - pin)  # includes thinking tokens
        return text, pin, pout

    def map(self, fn: Callable, items: list, concurrency: int | None = None) -> list:
        """Run fn over items with a thread pool (keeps order)."""
        n = concurrency or self.c.get("concurrency", 4)
        if n <= 1 or len(items) <= 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=n) as ex:
            return list(ex.map(fn, items))


class LLMHub:
    """Holds one client per role, sharing a tracker and cache."""

    def __init__(self, cfg: dict, run_dir: str | Path, mock_fn=None):
        self.cfg = cfg
        self.tracker = CostTracker(cfg.get("budget_usd", 5.0), Path(run_dir) / "usage.jsonl")
        self.cache = DiskCache(Path(cfg.get("cache_dir", ".cache")) / "llm_cache.sqlite")
        self.mock_fn = mock_fn
        self._clients: dict[str, LLM] = {}

    def __getitem__(self, role: str) -> LLM:
        if role not in self._clients:
            self._clients[role] = LLM(self.cfg, role, self.tracker, self.cache, self.mock_fn)
        return self._clients[role]

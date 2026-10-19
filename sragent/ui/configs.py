"""Config files for the web UI: list every run file under configs/, read, check and save YAML there, and
write new topics and run files from a short form.

Paths are given relative to the project root (the folder that holds configs/), e.g.
configs/review/<topic>/run.yaml. Only .yaml files inside configs/ can be read or written.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import yaml

from ..config import DEFAULTS, LLM_STAGES, default_run_dir, is_benchmark, load_catalog, load_config, role_cfg, stage_cfg
from ..config_lock import pending

KINDS = ("review", "benchmark")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
TOPIC_KEYS = {"key", "question", "seed_eligibility", "screening_rules", "records", "extraction_fields",
              "rob_tool_name", "rob_domains", "protocol_file"}
RUN_KEYS = set(DEFAULTS) | {"include", "topic"}
SECTIONS = ("pipeline", "feedback", "eval", "local")


def run_status(rd: Path) -> str:
    """What runs/ holds for a run file (also used by `sragent runs`)."""
    if not (rd / "store.json").exists():
        status = "not started"
    elif (rd / "report.md").exists():
        status = "review done"
    else:
        status = "review in progress"
    n_eval = len(list((rd / "eval" / "runs").glob("*/results.json")))
    return status + (f", evaluated {n_eval}x" if n_eval else "")


def _placeholders(text: str) -> list[str]:
    """Template placeholders such as <your_topic> still left in the values (comments are ignored)."""
    out = []
    for line in text.splitlines():
        s = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
        out += re.findall(r"<[^<>\n]{2,80}>", s)
    return list(dict.fromkeys(out))


def _q(s: str) -> str:
    """A YAML scalar for user text (quoted when needed)."""
    return yaml.safe_dump(s, default_style=None, width=10**6).strip().removesuffix("\n...").removesuffix("...").strip()


def _folded(s: str, indent: int = 4) -> str:
    """A long text as a folded block (`>-`), wrapped at about 100 columns."""
    words, lines, cur = " ".join(str(s).split()).split(" "), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > 100 - indent:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    lines.append(cur)
    pad = " " * indent
    return ">-\n" + "\n".join(pad + x for x in lines)


class Library:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.base = self.root.parent
        self.catalog_path = self.root / "models.yaml"   # the model catalog (provider and price per model)

    # ------------------------------------------------------------ paths
    def rel(self, p: str | os.PathLike) -> str:
        p = Path(p).resolve()
        try:
            return p.relative_to(self.base).as_posix()
        except ValueError:
            return str(p)

    def resolve(self, path: str, must_exist: bool = True) -> Path:
        """A .yaml file inside configs/; anything else is refused."""
        if not path:
            raise ValueError("no file given")
        p = Path(path).expanduser()
        p = (p if p.is_absolute() else self.base / p).resolve()
        if self.root not in p.parents:
            raise ValueError(f"only files inside {self.rel(self.root)}/ can be opened here")
        if p.suffix not in (".yaml", ".yml") or p.name.startswith("."):
            raise ValueError("only .yaml files can be opened here")
        if must_exist and not p.is_file():
            raise ValueError(f"{self.rel(p)} does not exist")
        return p

    def kind_of(self, p: Path) -> str:
        if p == self.catalog_path:
            return "catalog"
        if p.name == "topic.yaml":
            return "topic"
        parts = p.relative_to(self.root).parts
        if len(parts) >= 3 and parts[0] in KINDS:
            return "run"
        return "other"

    def _yaml_files(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*.y*ml")
                      if p.suffix in (".yaml", ".yml") and not any(x.startswith(".") for x in p.relative_to(self.root).parts))

    # ------------------------------------------------------------ listing
    def run_summary(self, p: Path) -> dict:
        parts = p.relative_to(self.root).parts
        row = {"path": self.rel(p), "kind": parts[0], "topic": "/".join(parts[1:-1]), "name": p.stem,
               "template": "_template" in parts, "mtime": p.stat().st_mtime}
        try:
            cfg = load_config(p)
        except Exception as e:
            return {**row, "error": f"{type(e).__name__}: {e}"[:300]}
        a = role_cfg(cfg, "agent")
        rd = Path(cfg["run_dir"]).resolve()
        topic = cfg.get("topic") or {}
        locked = [c["key"] for c in pending(cfg, rd)]
        return {**row, "config_changed": locked, "agent": f"{a.get('provider')}:{a.get('model')}", "human_mode": cfg["pipeline"]["human_mode"],
                "records": (topic.get("records") or {}).get("source", ""), "max_records": cfg["pipeline"]["max_records"],
                "benchmark": is_benchmark(cfg), "question": topic.get("question", ""), "budget_usd": cfg.get("budget_usd"),
                "run_dir": self.rel(rd), "status": run_status(rd), "spent_usd": _spent(rd)}

    def listing(self) -> dict:
        runs, topics, models, other = [], [], [], []
        for p in self._yaml_files():
            k = self.kind_of(p)
            if k == "run":
                runs.append(self.run_summary(p))
            elif k == "topic":
                parts = p.relative_to(self.root).parts
                try:
                    t = (yaml.safe_load(p.read_text()) or {}).get("topic") or {}
                except yaml.YAMLError:
                    t = {}
                topics.append({"path": self.rel(p), "kind": parts[0] if parts[0] in KINDS else "",
                               "folder": self.rel(p.parent), "topic": "/".join(parts[1:-1]),
                               "question": t.get("question", "") if isinstance(t, dict) else "",
                               "source": ((t.get("records") or {}).get("source", "") if isinstance(t, dict) else ""),
                               "template": "_template" in parts})
            elif k == "catalog":
                continue
            else:
                other.append({"path": self.rel(p)})
        try:
            models = self.catalog_models()
            cat_error = None
        except ValueError as e:
            models, cat_error = [], str(e)
        return {"root": self.rel(self.root), "runs": runs, "topics": topics, "models": models, "other": other,
                "catalog": self.rel(self.catalog_path), "catalog_error": cat_error}

    def catalog_models(self) -> list[dict]:
        """Every model in the catalog with its provider and price (raises ValueError if the file is invalid)."""
        out = []
        for name, e in load_catalog(self.catalog_path).items():
            pr = e.get("pricing") or {}
            out.append({"name": name, "provider": e["provider"], "input_per_m": pr.get("input_per_m"),
                        "output_per_m": pr.get("output_per_m"), "long_context": bool(pr.get("long_context"))})
        return out

    # ------------------------------------------------------------ read / check / save
    def read(self, path: str) -> dict:
        p = self.resolve(path)
        return {"path": self.rel(p), "kind": self.kind_of(p), "text": p.read_text(), "mtime": p.stat().st_mtime}

    def check(self, path: str, text: str, must_exist: bool = False) -> dict:
        """Parse the text and, for a run file, load it with its includes exactly as `sragent run` would."""
        p = self.resolve(path, must_exist=must_exist)
        kind = self.kind_of(p)
        errors, warnings, out = [], [], {"path": self.rel(p), "kind": kind}
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            return {**out, "ok": False, "errors": [f"YAML syntax: {getattr(e, 'problem', None) or e}"
                                                   + (f" (line {mark.line + 1}, column {mark.column + 1})" if mark else "")],
                    "warnings": [], "line": mark.line + 1 if mark else None}
        data = data if data is not None else {}
        if not isinstance(data, dict):
            return {**out, "ok": False, "errors": ["the file must be a mapping of keys (key: value)"], "warnings": []}
        ph = _placeholders(text)
        if ph:
            warnings.append("template placeholders still to fill in: " + ", ".join(ph[:8]) + (" …" if len(ph) > 8 else ""))
        if kind == "topic":
            self._check_topic(data, errors, warnings)
            if isinstance(data.get("topic"), dict):
                out["locked"] = self._topic_locked(p, data["topic"])
        elif kind == "catalog":
            tmp = p.parent / f".{p.stem}.check-{os.getpid()}-{time.time_ns()}.yaml"
            try:
                tmp.write_text(text)
                names = set(load_catalog(tmp))
            except ValueError as e:
                errors.append(str(e).replace(str(tmp), self.rel(p)))
                names = None
            finally:
                tmp.unlink(missing_ok=True)
            if names is not None:   # models the run files use but the new text no longer lists
                used = {}
                for rp in self._yaml_files():
                    if self.kind_of(rp) == "run":
                        try:
                            c = load_config(rp)
                        except Exception:
                            continue
                        for m in self._models_of(c):
                            used.setdefault(m, []).append(rp.stem)
                gone = {m: r for m, r in used.items() if m not in names and m in (load_catalog(p) if p.exists() else {})}
                for m, r in gone.items():
                    errors.append(f"{m} is still used by {', '.join(sorted(set(r))[:4])}; removing it leaves them without a price")
        elif kind == "run":
            self._check_run(p, text, data, errors, warnings, out)
        return {**out, "ok": not errors, "errors": errors, "warnings": warnings}

    def _topic_locked(self, p: Path, topic: dict) -> list[dict]:
        """Started run files in this folder whose fixed settings this topic text would change."""
        out = []
        for rp in sorted(p.parent.glob("*.yaml")):
            if rp == p or self.kind_of(rp) != "run":
                continue
            try:
                cfg = load_config(rp)
            except Exception:
                continue
            if Path(cfg["run_dir"]).joinpath("store.json").exists():
                ch = pending({**cfg, "topic": {**(cfg.get("topic") or {}), **topic}}, cfg["run_dir"])
                if ch:
                    out.append({"run": self.rel(rp), "changes": ch})
        return out

    @staticmethod
    def _check_topic(data: dict, errors: list, warnings: list) -> None:
        t = data.get("topic")
        if not isinstance(t, dict):
            errors.append("a topic file holds one key, topic:, with the review question, criteria, records and fields")
            return
        if set(data) - {"topic"}:
            warnings.append("a topic file only holds topic:; also found: " + ", ".join(sorted(set(data) - {"topic"})))
        for k in ("key", "question", "seed_eligibility"):
            if not t.get(k) and not (k == "seed_eligibility" and t.get("protocol_file")):
                errors.append(f"topic.{k} is missing")
        unknown = set(t) - TOPIC_KEYS
        if unknown:
            warnings.append("unknown topic keys (ignored): " + ", ".join(sorted(unknown)))
        rec = t.get("records") or {}
        src = rec.get("source")
        if src not in ("pubmed", "file", "synergy"):
            errors.append("topic.records.source must be pubmed, file or synergy")
        if src == "file" and not rec.get("file"):
            errors.append("topic.records.file is needed with source: file (a JSONL path)")
        if src == "synergy" and not rec.get("synergy_key"):
            errors.append("topic.records.synergy_key is needed with source: synergy")
        for i, f in enumerate(t.get("extraction_fields") or []):
            if not isinstance(f, dict) or not f.get("name"):
                errors.append(f"topic.extraction_fields[{i}] needs a name")
            elif f.get("kind") not in (None, "drug", "disease"):
                warnings.append(f"extraction field {f['name']}: kind is drug or disease (or left out)")
        if not t.get("extraction_fields"):
            warnings.append("no extraction_fields: nothing will be extracted from included studies")
        for i, d in enumerate(t.get("rob_domains") or []):
            if not isinstance(d, dict) or not d.get("name"):
                errors.append(f"topic.rob_domains[{i}] needs a name")

    def _check_run(self, p: Path, text: str, data: dict, errors: list, warnings: list, out: dict) -> None:
        unknown = set(data) - RUN_KEYS
        if unknown:
            warnings.append("unknown keys (ignored): " + ", ".join(sorted(unknown)))
        for sec in SECTIONS:
            if isinstance(data.get(sec), dict):
                bad = set(data[sec]) - set(DEFAULTS[sec])
                if bad:
                    warnings.append(f"unknown {sec} keys (ignored): " + ", ".join(sorted(bad)))
        for inc in data.get("include") or []:
            if not (p.parent / inc).resolve().is_file():
                errors.append(f"include: {inc} not found (paths are relative to this file's folder)")
        if errors:
            return
        # load from a hidden sibling so relative includes resolve as they will for the saved file
        tmp = p.parent / f".{p.stem}.check-{os.getpid()}-{time.time_ns()}.yaml"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text)
            cfg = load_config(tmp)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}"[:500])
            return
        finally:
            tmp.unlink(missing_ok=True)
        if not data.get("run_dir"):
            cfg["run_dir"] = default_run_dir(p)
        topic = cfg.get("topic") or {}
        if not topic.get("question"):
            errors.append("no topic: include the folder's topic.yaml (or write topic: here)")
        tmp_errors: list = []
        self._check_topic({"topic": topic}, tmp_errors, [])
        errors += [e for e in tmp_errors if e not in errors]
        ph = _placeholders(yaml.safe_dump(topic, width=10**6))
        if ph and "topic" not in data:
            warnings.append("the included topic still has template placeholders: " + ", ".join(ph[:6]) + (" …" if len(ph) > 6 else ""))
        parts = p.relative_to(self.root).parts
        bench = is_benchmark(cfg)
        if parts[0] == "review" and bench:
            warnings.append("records.source: synergy is a benchmark topic; it belongs under configs/benchmark/")
        if parts[0] == "benchmark" and not bench:
            warnings.append("a topic without known answers belongs under configs/review/")
        if not bench and (topic.get("records") or {}).get("source") == "pubmed" and not cfg.get("contact_email"):
            warnings.append("contact_email is empty: Unpaywall open-access full texts need it")
        cat = cfg.get("_catalog") or {}
        unpriced = sorted({m for m in self._models_of(cfg) if m not in cat})
        for m in unpriced:
            warnings.append(f"{m} is not in {self.rel(self.catalog_path)}: its calls are counted as free by the "
                            "budget guard and the estimates (add it there, or write its pricing)")
        a, j = role_cfg(cfg, "agent"), role_cfg(cfg, "judge")
        rd = Path(cfg["run_dir"]).resolve()
        ch = pending(cfg, rd)
        if ch:
            out["locked"] = [{"run": self.rel(p), "changes": ch}]
        out["summary"] = {"run_dir": self.rel(rd), "agent": f"{a.get('provider')}:{a.get('model')}",
                          "judge": f"{j.get('provider')}:{j.get('model')}", "human_mode": cfg["pipeline"]["human_mode"],
                          "records": (topic.get("records") or {}).get("source", ""), "benchmark": bench,
                          "budget_usd": cfg.get("budget_usd"), "max_records": cfg["pipeline"]["max_records"],
                          "status": run_status(rd), "question": topic.get("question", "")}
        shown = {k: v for k, v in cfg.items() if not k.startswith("_")}
        out["resolved"] = yaml.safe_dump(shown, sort_keys=False, allow_unicode=True, width=110)

    @staticmethod
    def _models_of(cfg: dict) -> set:
        """Names of the API models a run file uses (roles it uses and every stage), without priced ones it writes itself."""
        out = set()
        for c in [role_cfg(cfg, r) for r in ("agent", "judge", "reviewer", "judge2", "verifier")] + \
                 [stage_cfg(cfg, st) for st in LLM_STAGES]:
            pr = c.get("pricing") or {}
            if c.get("provider") not in ("local", "mock") and c.get("model") and \
                    (c["model"] in (cfg.get("_catalog") or {}) or not (pr.get("input_per_m") or pr.get("output_per_m"))):
                out.add(c["model"])
        return out

    def save(self, path: str, text: str, mtime: float | None = None, create: bool = False) -> dict:
        p = self.resolve(path, must_exist=False)
        if create and p.exists():
            return {"ok": False, "message": f"{self.rel(p)} already exists"}
        if not create and not p.exists():
            return {"ok": False, "message": f"{self.rel(p)} does not exist"}
        if mtime and p.exists() and abs(p.stat().st_mtime - float(mtime)) > 1e-6:
            return {"ok": False, "conflict": True,
                    "message": "the file was changed on disk after you opened it; reload it (or save again to overwrite)"}
        res = self.check(path, text)
        if any(e.startswith("YAML syntax") or e.startswith("the file must be") for e in res["errors"]):
            return {**res, "ok": False, "message": "not saved: " + res["errors"][0]}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if text.endswith("\n") else text + "\n")
        msg = "saved" + (f" with {len(res['errors'])} problem(s) to fix" if res["errors"] else "")
        return {**res, "ok": True, "saved": True, "message": msg, "mtime": p.stat().st_mtime, "path": self.rel(p)}

    @staticmethod
    def set_block(text: str, key: str, value: dict, comment: str = "") -> str:
        """Replace (or add, or remove when empty) one top-level mapping in YAML text, keeping the rest of the
        file, comments included, as written. Each entry is written on one line in flow style."""
        lines = text.splitlines()
        i = next((n for n, l in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", l)), None)
        block = []
        if value:
            block = [f"{key}:" + (f"                 # {comment}" if comment else "")]
            for k, v in value.items():
                block.append(f"  {k}: " + yaml.safe_dump(v, default_flow_style=True, width=10**6).strip())
        if i is None:
            if not block:
                return text
            while lines and not lines[-1].strip():
                lines.pop()
            return "\n".join(lines + block) + "\n"
        j = i + 1
        while j < len(lines) and (lines[j][:1] in (" ", "\t") or (not lines[j].strip() and j + 1 < len(lines)
                                                                   and lines[j + 1][:1] in (" ", "\t"))):
            j += 1
        return "\n".join(lines[:i] + block + lines[j:]) + "\n"

    # ------------------------------------------------------------ new files
    def _models_block(self, kind: str, folder: Path, b: dict) -> tuple[list[str], str]:
        """llm_defaults (the agent) and, for a benchmark, the judge: chosen by name from the catalog."""
        names = {m["name"] for m in self.catalog_models()}
        agent = b.get("agent_model") or ""
        effort = b.get("agent_effort") or "low"
        env = Path(os.path.relpath(self.base / "api_env.yaml", folder)).as_posix()
        if agent == "local":
            lines = ["llm_defaults:                # your vLLM servers (local_endpoints.yaml); free",
                     "  provider: local", "  model: auto", f"  api_env: {env}   # optional local_api_key",
                     "  reasoning_effort: null", "  max_tokens: 16000", "  concurrency: 32"]
        else:
            if agent not in names:
                raise ValueError(f"choose an agent model from {self.rel(self.catalog_path)}")
            lines = ["llm_defaults:                # models by name; provider and price come from configs/models.yaml",
                     f"  model: {agent}      # the agent, and every role and stage that does not say otherwise",
                     f"  api_env: {env}", f"  reasoning_effort: {effort}", "  max_tokens: 16000", "  concurrency: 6"]
        judge = b.get("judge_model") or ""
        if kind == "benchmark" and judge:
            if judge not in names:
                raise ValueError(f"choose a judge model from {self.rel(self.catalog_path)}")
            lines += ["roles:                         # evaluation roles (sragent eval)",
                      f"  judge: {{model: {judge}, reasoning_effort: medium}}"]
        return lines, ("local" if agent == "local" else agent)

    @staticmethod
    def _name(s: str, what: str) -> str:
        s = (s or "").strip().removesuffix(".yaml")
        if not NAME_RE.match(s) or s.startswith("_"):
            raise ValueError(f"{what}: use letters, digits, _ . - (not starting with _)")
        return s

    def run_text(self, kind: str, folder: Path, b: dict) -> str:
        model_lines, models = self._models_block(kind, folder, b)
        mode = b.get("human_mode") or "human_in_the_loop"
        if mode not in ("human_in_the_loop", "agent_only"):
            raise ValueError("human_mode is human_in_the_loop or agent_only")
        # still unclear after the full text: a person decides in human_in_the_loop; agent_only needs a rule
        unclear = (b.get("fulltext_unclear") or "include") if mode == "agent_only" else "await"
        if mode == "agent_only" and unclear == "await":
            raise ValueError("with agent_only, still-unclear full texts must be included or excluded (no person decides)")
        mr = b.get("max_records")
        mr = int(mr) if mr not in (None, "", 0, "0") else None
        if mr and kind != "benchmark":
            raise ValueError("max_records (a sample) is for benchmark topics only")
        people = "agent only" if mode == "agent_only" else "human in the loop"
        lines = [f"# {kind.capitalize()} run: {folder.name} · {models} · {mr or 'all'} records · {people}.",
                 "include:", "  - topic.yaml", *model_lines,
                 "cache_dir: .cache",
                 f"budget_usd: {float(b.get('budget_usd') or 10.0):g}           # hard cost cap for this run folder",
                 f"contact_email: {_q(b.get('contact_email') or '')}          # Unpaywall open-access full texts need it",
                 "pipeline:", f"  human_mode: {mode}"]
        if unclear != "await":
            lines.append(f"  fulltext_unclear: {unclear}")
        if mr:
            lines.append(f"  max_records: {mr}")
        if kind == "benchmark":
            lines += ["eval:                      # for `sragent eval`", f"  budget_usd: {float(b.get('eval_budget_usd') or 8.0):g}"]
        return "\n".join(lines) + "\n"

    def new_run(self, b: dict) -> dict:
        """A run file in an existing topic folder."""
        tp = self.resolve(b.get("topic_path") or "")
        if self.kind_of(tp) != "topic":
            raise ValueError("choose a topic folder (one with a topic.yaml)")
        folder = tp.parent
        kind = folder.relative_to(self.root).parts[0]
        name = self._name(b.get("name"), "run file name")
        p = folder / f"{name}.yaml"
        if p.exists():
            raise ValueError(f"{self.rel(p)} already exists")
        p.write_text(self.run_text(kind, folder, b))
        return {"ok": True, "path": self.rel(p), "files": [self.rel(p)], "check": self.check(self.rel(p), p.read_text())}

    def new_topic(self, b: dict) -> dict:
        """A review topic folder: topic.yaml written from the form, plus its first run file."""
        key = self._name(b.get("key"), "topic folder")
        if "." in key:
            raise ValueError("topic folder: use letters, digits, _ and - only")
        folder = self.root / "review" / key
        if folder.exists():
            raise ValueError(f"{self.rel(folder)} already exists")
        question, elig = (b.get("question") or "").strip(), (b.get("seed_eligibility") or "").strip()
        if not question or not elig:
            raise ValueError("write the review question and the eligibility criteria")
        src = b.get("source") or "pubmed"
        L = [f"# Review topic: {key}. Edit freely; `sragent ui` checks this file when you save it.",
             "topic:", f"  key: {key}", f"  question: {_folded(question)}",
             "  # Plain-words eligibility; the agent turns it into atomic criteria you approve before screening.",
             f"  seed_eligibility: {_folded(elig)}",
             "  # Deterministic exclusions before any LLM call (sragent/rules.py).",
             f"  screening_rules: [{', '.join(b.get('screening_rules') or ['empty_record', 'conference_abstract', 'erratum', 'editorial', 'review', 'off_topic_no_abstract'])}]",
             "  records:"]
        if src == "pubmed":
            L += ["    source: pubmed           # the agent writes the PubMed query",
                  f"    pubmed_retmax: {int(b.get('pubmed_retmax') or 1000)}      # most records to retrieve",
                  f"    maxdate: {json.dumps(b.get('maxdate') or time.strftime('%Y/%m/%d'))}  # search end date"]
        elif src == "file":
            if not b.get("records_file"):
                raise ValueError("give the JSONL file with your records (path from the project root)")
            L += ["    source: file             # your own export, one JSON record per line",
                  f"    file: {_q(b['records_file'])}"]
            if b.get("maxdate"):
                L.append(f"    maxdate: {json.dumps(b['maxdate'])}  # used for citation chasing")
        else:
            raise ValueError("records source is pubmed or file")
        fields = _parse_items(b.get("extraction_fields") or "", with_kind=True)
        L.append("  extraction_fields:         # kind: drug | disease turns on database term checks")
        L += [f"    - {{{_flow(f)}}}" for f in fields] or ["    - {name: outcomes, description: \"Main results with numbers and units.\"}"]
        L.append(f"  rob_tool_name: {_q(b.get('rob_tool_name') or 'RoB 2')}")
        domains = _parse_items(b.get("rob_domains") or "", with_kind=False)
        L.append("  rob_domains:")
        L += [f"    - {{{_flow(d)}}}" for d in domains] or ["    - {name: overall, description: \"Overall risk of bias.\"}"]
        run_name = self._name(b.get("run_name") or "run", "run file name")
        folder.mkdir(parents=True)
        tp = folder / "topic.yaml"
        tp.write_text("\n".join(L) + "\n")
        try:
            rp = folder / f"{run_name}.yaml"
            rp.write_text(self.run_text("review", folder, b))
        except Exception:
            tp.unlink()
            folder.rmdir()
            raise
        return {"ok": True, "path": self.rel(rp), "files": [self.rel(tp), self.rel(rp)],
                "check": self.check(self.rel(rp), rp.read_text())}

    def duplicate(self, b: dict) -> dict:
        src = self.resolve(b.get("path") or "")
        name = self._name(b.get("name"), "new file name")
        p = src.parent / f"{name}.yaml"
        if p.exists():
            raise ValueError(f"{self.rel(p)} already exists")
        p.write_text(src.read_text())
        return {"ok": True, "path": self.rel(p), "files": [self.rel(p)]}


def _parse_items(text: str, with_kind: bool) -> list[dict]:
    """Form lines `name: description` (extraction fields may add a kind: `name [drug]: description`)."""
    out = []
    for line in str(text).splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        name, _, desc = line.partition(":")
        m = re.match(r"^(.*?)\s*\[(drug|disease)\]\s*$", name)
        kind = None
        if m:
            name, kind = m.group(1), m.group(2)
        name = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip()).strip("_").lower()
        if not name:
            continue
        d = {"name": name}
        if with_kind and kind:
            d["kind"] = kind
        d["description"] = desc.strip()
        out.append(d)
    return out


def _flow(d: dict) -> str:
    return ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False) if k == 'description' else v}" for k, v in d.items())


def _spent(rd: Path) -> float:
    tot = 0.0
    for p in [rd / "usage.jsonl", *sorted((rd / "eval" / "runs").glob("*/usage.jsonl"))]:
        if p.exists():
            for line in p.read_text().splitlines():
                try:
                    tot += float(json.loads(line).get("usd", 0))
                except ValueError:
                    pass
    return round(tot, 4)

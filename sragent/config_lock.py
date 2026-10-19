"""Settings that define a review are fixed once it has started.

The first time a review is saved, the review-defining settings (LOCKED: the topic, the human-involvement
condition, sampling) are recorded in store.json (meta.config_lock). Every later load compares the
current config with that record and refuses to continue when they differ, whichever way the config was
edited (web interface, text editor, a changed include, --run-dir onto an existing folder). Nothing is
spent and no output is mixed.

Each stage's agent model and effort (stage_models, else the agent role) is fixed the same way once that
stage has started (STAGE_STARTED); the model of a stage that has not started can change freely.

Three ways on: revert the edit; make a new run file (its own runs/ folder); or accept the change on
purpose (`sragent accept-config --reason ...` or the web interface). An accepted change is written to
meta.config_changes and to provenance.jsonl, and the report and every evaluation of the run show it.
Other settings (budget, contact email, the models of stages not yet started, feedback handling, the judges,
feedback.*, eval.*) can change at any time.
"""
from __future__ import annotations

import copy
import json
import time

LOCKED = (
    "topic.key", "topic.question", "topic.seed_eligibility", "topic.protocol_file", "topic.screening_rules",
    "topic.records", "topic.extraction_fields", "topic.rob_tool_name", "topic.rob_domains",
    "pipeline.human_mode", "pipeline.max_records", "pipeline.screen_sample_seed", "pipeline.fulltext_unclear",
)


STAGE_STARTED = {   # a stage has started when its first output exists
    "plan": lambda st: st.get("protocol") is not None,
    "search": lambda st: st.get("search") is not None,
    "screen": lambda st: bool(st.by_type("screen")),
    "fulltext": lambda st: bool(st.by_type("fulltext")) or any(
        n["data"].get("stage") in ("full_text", "retrieval") for n in st.by_type("screen")),
    "snowball": lambda st: bool(st.meta.get("snowball")),
    "extract": lambda st: bool(st.by_type("extraction")),
    "rob": lambda st: bool(st.by_type("rob")),
    "synthesize": lambda st: bool(st.by_type("synthesis")),
}


def stage_model(cfg: dict, stage: str) -> dict:
    from .config import stage_cfg
    c = stage_cfg(cfg, stage)
    return {"provider": c.get("provider"), "model": c.get("model"), "reasoning_effort": c.get("reasoning_effort")}


def started_stages(store) -> list[str]:
    return [s for s, f in STAGE_STARTED.items() if f(store)]


class ConfigChanged(Exception):
    """The config differs from the settings recorded when the review started."""

    def __init__(self, changes: list[dict], run_dir=None):
        self.changes, self.run_dir = changes, run_dir
        keys = ", ".join(c["key"] for c in changes)
        super().__init__(
            f"settings that define this review were changed after it started: {keys}. "
            "Revert the edit, make a new run file for the changed design, or accept the change on purpose "
            "(sragent accept-config --config <run file> --reason \"...\", or Accept in the web interface; "
            "it is recorded and shown in the report and evaluations).")


def _get(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def locked_values(cfg: dict, stages=()) -> dict:
    """The review-defining settings as plain JSON values (the effective value where a default applies),
    plus the model of each given (started) stage as stage_models.<stage>."""
    out = {f"stage_models.{s}": stage_model(cfg, s) for s in stages}
    for k in LOCKED:
        v = _get(cfg, k)
        if k == "topic.screening_rules" and v is None:
            from .rules import DEFAULT_RULES
            v = list(DEFAULT_RULES)
        out[k] = json.loads(json.dumps(v, default=str))
    return out


def diff(recorded: dict, cfg: dict) -> list[dict]:
    stages = [k.split(".", 1)[1] for k in recorded if k.startswith("stage_models.")]
    now = locked_values(cfg, stages)
    keys = [k for k in LOCKED if k in recorded] + [f"stage_models.{s}" for s in STAGE_STARTED if f"stage_models.{s}" in recorded]
    return [{"key": k, "old": recorded.get(k), "new": now[k]} for k in keys if recorded.get(k) != now[k]]


def check(cfg: dict, store, run_dir=None) -> None:
    """Raise ConfigChanged when a started review's config no longer matches what it started with."""
    rec = (store.meta.get("config_lock") or {}).get("values")
    if rec:
        ch = diff(rec, cfg)
        if ch:
            raise ConfigChanged(ch, run_dir)


def record_if_new(cfg: dict, store) -> None:
    """Called on every save: the first save of a review records its settings, and the first save after a
    stage has started records that stage's model."""
    lock = store.meta.get("config_lock")
    if not lock:
        lock = store.meta["config_lock"] = {"recorded": time.time(), "values": locked_values(cfg)}
    new = [s for s in started_stages(store) if f"stage_models.{s}" not in lock["values"]]
    if new:   # only the newly started stages; the settings recorded earlier are never overwritten here
        lock["values"].update({f"stage_models.{s}": stage_model(cfg, s) for s in new})


def accept(ctx, reason: str, actor: str = "user") -> list[dict]:
    """Accept the current config on purpose: record what changed and why, then continue with it."""
    from . import provenance
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("give a reason for changing the review's settings; it is recorded and shown in the report")
    rec = (ctx.store.meta.get("config_lock") or {}).get("values")
    if not rec:
        return []
    ch = diff(rec, ctx.cfg)
    if not ch:
        return []
    entry = {"ts": time.time(), "actor": actor, "reason": reason, "changes": copy.deepcopy(ch)}
    ctx.store.meta.setdefault("config_changes", []).append(entry)
    stages = [k.split(".", 1)[1] for k in rec if k.startswith("stage_models.")]
    ctx.store.meta["config_lock"] = {"recorded": time.time(), "values": locked_values(ctx.cfg, stages)}
    provenance.record(ctx, "config_change", "accepted", reason=reason, changes=ch)
    ctx.save()
    ctx.log(f"[config] accepted changed settings ({', '.join(c['key'] for c in ch)}): {reason}")
    return ch


def read_meta(store_path) -> dict:
    """store.json's meta without parsing the (large) nodes: it is written first."""
    from pathlib import Path
    p = Path(store_path)
    if not p.exists():
        return {}
    with open(p) as f:
        head = f.read(1 << 21)
    i = head.find('"meta":')
    if i >= 0:
        try:
            return json.JSONDecoder().raw_decode(head, head.index("{", i))[0]
        except ValueError:
            pass
    try:
        return json.loads(p.read_text()).get("meta", {})
    except ValueError:
        return {}


def pending(cfg: dict, run_dir) -> list[dict]:
    """Changes not yet accepted for the review in run_dir ([] when it has not started)."""
    from pathlib import Path
    rec = (read_meta(Path(run_dir) / "store.json").get("config_lock") or {}).get("values")
    return diff(rec, cfg) if rec else []


def models_by_stage(cfg: dict, store) -> list[tuple[str, str]]:
    """[(stages, "model (effort)")] grouped: the model each started stage ran with (as recorded when it
    started), else the configured one."""
    from .config import STAGE_LABELS
    rec = (store.meta.get("config_lock") or {}).get("values") or {}
    groups: dict[str, list[str]] = {}
    for st in STAGE_STARTED:
        m = rec.get(f"stage_models.{st}") or stage_model(cfg, st)
        key = f"{m.get('model')}" + (f" ({m['reasoning_effort']} effort)" if m.get("reasoning_effort") else "")
        groups.setdefault(key, []).append(STAGE_LABELS[st].lower())
    return [(", ".join(v), k) for k, v in groups.items()]


def changes(store) -> list[dict]:
    return list(store.meta.get("config_changes") or [])


def short(v, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"

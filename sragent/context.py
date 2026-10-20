"""Run context shared by all stages."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMHub
from .sources import http, pubmed
from .store import ReviewStore


class StageAgent:
    """ctx.agent: picks the agent client per call, for the current pipeline stage or, outside a stage
    (sync, re-runs, feedback), for the stage the call's tag belongs to (config.stage_of_tag)."""

    def __init__(self, ctx: "Ctx"):
        self._ctx = ctx

    def client(self, tag: str = ""):
        from .config import stage_of_tag
        return self._ctx.hub.agent_for(self._ctx.stage or stage_of_tag(tag))

    def chat(self, messages, **kw):
        return self.client(kw.get("tag", "")).chat(messages, **kw)

    def map(self, fn, items, concurrency=None):
        return self.client().map(fn, items, concurrency)

    def __getattr__(self, name):
        return getattr(self.client(), name)


@dataclass
class Ctx:
    cfg: dict
    run_dir: Path
    hub: LLMHub
    store: ReviewStore
    topic: dict = field(default_factory=dict)
    quiet: bool = False
    note: str = ""          # reviewer note added to every agent prompt (user-requested re-runs)
    stage: str | None = None   # the pipeline stage being run: its agent model comes from stage_models

    @classmethod
    def create(cls, cfg: dict, run_dir: str | Path | None = None, mock_fn=None, fresh: bool = False,
               check_config: bool = True):
        """check_config=False only for accepting changed settings (config_lock.accept)."""
        from . import config_lock
        rd = Path(run_dir or cfg["run_dir"])
        rd.mkdir(parents=True, exist_ok=True)
        http.init_cache(cfg.get("cache_dir", ".cache"))
        pubmed.configure(cfg.get("contact_email", ""), cfg.get("ncbi_api_key", ""))
        sp = rd / "store.json"
        store = ReviewStore.load(sp) if (sp.exists() and not fresh) else ReviewStore(rd)
        store.run_dir = rd
        if check_config:   # a started review refuses a config whose review-defining settings changed
            config_lock.check(cfg, store, rd)
        from . import graph
        for what in graph.migrate(store, cfg):     # an older store: versioned edges, settings nodes
            print(f"[store] migrated to schema {graph.SCHEMA}: {what}", flush=True)
        if not fresh or not sp.exists():
            graph.sync_settings(store, cfg)        # the review's settings as nodes (versions move only on change)
        store.meta.setdefault("schema", graph.SCHEMA)
        hub = LLMHub(cfg, rd, mock_fn=mock_fn)
        return cls(cfg=cfg, run_dir=rd, hub=hub, store=store, topic=cfg.get("topic", {}))

    def log(self, *a):
        msg = " ".join(str(x) for x in a)
        if not self.quiet:
            print(msg, flush=True)
        with open(self.run_dir / "run.log", "a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")

    def save(self):
        # always save into THIS context's run_dir (eval copies must never overwrite the main store)
        from .config_lock import record_if_new
        record_if_new(self.cfg, self.store)   # the first save fixes the review-defining settings
        self.store.save(self.run_dir / "store.json")
        (self.run_dir / "cost.json").write_text(json.dumps(self.hub.tracker.summary(), indent=1))

    def map(self, fn, items):
        """Parallel map over items; the store is saved even if a call fails midway (e.g. quota),
        so finished work is never lost and the stage resumes where it stopped."""
        try:
            return self.agent.map(fn, items)
        finally:
            self.save()

    @property
    def agent(self):
        return StageAgent(self)

    @contextmanager
    def at(self, stage: str):
        """Run a block as one pipeline stage (its LLM calls use that stage's model)."""
        prev, self.stage = self.stage, stage
        try:
            yield self
        finally:
            self.stage = prev

    def msgs(self, user: str, system: str | None = None):
        from .prompts import SYSTEM_AGENT
        if self.note:
            user += ("\n\nREVIEWER NOTE (a human reviewer asked for this re-run; take it into account, but stay "
                     "faithful to the source text and cite sentence ids as usual):\n" + self.note)
        return [{"role": "system", "content": system or SYSTEM_AGENT}, {"role": "user", "content": user}]

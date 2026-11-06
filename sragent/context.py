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


class StageWriter:
    """ctx.writer: the writer of the stage a call belongs to: the stage its tag names (stagespec.stage_of_tag), else
    the stage being run, else feedback. An extraction redone during a screening run is the extraction writer's."""

    def __init__(self, ctx: "Ctx"):
        self._ctx = ctx

    def client(self, tag: str = ""):
        from .stagespec import FEEDBACK, stage_of_tag
        s = stage_of_tag(tag)
        return self._ctx.hub.model(s if s != FEEDBACK else (self._ctx.stage or FEEDBACK))

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
    stage: str | None = None   # the pipeline stage being run (models.<stage>.writer for calls whose tag names no stage)

    @classmethod
    def create(cls, cfg: dict, run_dir: str | Path | None = None, mock_fn=None, fresh: bool = False,
               check_config: bool = True):
        """check_config=False only for accepting changed settings (config_lock.accept)."""
        from . import config_lock
        rd = Path(run_dir or cfg["run_dir"])
        rd.mkdir(parents=True, exist_ok=True)
        http.init_cache(cfg.get("cache_dir", ".cache"))
        from .config import load_secrets
        sec = load_secrets(cfg["llm_defaults"].get("api_env"), cfg.get("_config_dir")) or {}
        pubmed.configure(cfg.get("contact_email") or sec.get("contact_email") or "", cfg.get("ncbi_api_key") or sec.get("ncbi_api_key") or "")
        sp = rd / "store.json"
        store = ReviewStore.load(sp) if (sp.exists() and not fresh) else ReviewStore(rd)
        store.run_dir = rd
        from . import graph
        if store.nodes and store.meta.get("schema") != graph.SCHEMA:
            raise ValueError(f"{sp} was made by an older version (format {store.meta.get('schema')}, this version reads "
                             f"{graph.SCHEMA}); run the review again in a new run folder")
        if check_config:   # a started review refuses a config whose review-defining settings changed
            config_lock.check(cfg, store, rd)
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

    SAVE_EVERY = 24   # items per save during a long stage: progress shows in the interface and survives a stop

    def map(self, fn, items):
        """Parallel map over items, in chunks with a save after each, so the interface shows progress during a long
        stage and finished work is never lost if a call fails midway (e.g. quota); the stage resumes where it stopped."""
        items = list(items)
        out = []
        try:
            for i in range(0, len(items), self.SAVE_EVERY):
                out += self.writer.map(fn, items[i:i + self.SAVE_EVERY])
                if i + self.SAVE_EVERY < len(items):
                    self.save()
            return out
        finally:
            self.save()

    @property
    def writer(self):
        return StageWriter(self)

    @contextmanager
    def at(self, stage: str):
        """Run a block as one pipeline stage (calls whose tag names no stage use that stage's writer)."""
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

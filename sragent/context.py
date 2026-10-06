"""Run context shared by all stages."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMHub
from .sources import http, pubmed
from .store import ReviewStore


@dataclass
class Ctx:
    cfg: dict
    run_dir: Path
    hub: LLMHub
    store: ReviewStore
    topic: dict = field(default_factory=dict)
    quiet: bool = False
    note: str = ""          # reviewer note added to every agent prompt (user-requested re-runs)

    @classmethod
    def create(cls, cfg: dict, run_dir: str | Path | None = None, mock_fn=None, fresh: bool = False):
        rd = Path(run_dir or cfg["run_dir"])
        rd.mkdir(parents=True, exist_ok=True)
        http.init_cache(cfg.get("cache_dir", ".cache"))
        pubmed.configure(cfg.get("contact_email", ""), cfg.get("ncbi_api_key", ""))
        sp = rd / "store.json"
        store = ReviewStore.load(sp) if (sp.exists() and not fresh) else ReviewStore(rd)
        store.run_dir = rd
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
        return self.hub["agent"]

    def msgs(self, user: str, system: str | None = None):
        from .prompts import SYSTEM_AGENT
        if self.note:
            user += ("\n\nREVIEWER NOTE (a human reviewer asked for this re-run; take it into account, but stay "
                     "faithful to the source text and cite sentence ids as usual):\n" + self.note)
        return [{"role": "system", "content": system or SYSTEM_AGENT}, {"role": "user", "content": user}]

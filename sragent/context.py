"""Run context shared by all stages."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMHub, Refused, UnusableReply
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


class Progress:
    """How far a long step is: <run folder>/progress.json, {label, done, total, started, updated}, written at most
    every few seconds (and at the end). The interface shows it beside the running job."""
    EVERY = 2.0

    def __init__(self, ctx: "Ctx", label: str | None, total: int):
        from .stagespec import BY_KEY, label as stage_label
        self.path = ctx.run_dir / "progress.json"
        self.label = label or (stage_label(ctx.stage) if ctx.stage in BY_KEY else "Working")
        self.total, self.done = total, 0
        self.started = self.written = time.time()
        self._lock = threading.Lock()
        self.write()

    def tick(self) -> None:
        with self._lock:
            self.done += 1
            due = time.time() - self.written >= self.EVERY
        if due:
            self.write()

    def write(self) -> None:
        with self._lock:
            self.written = time.time()
            d = {"label": self.label, "done": self.done, "total": self.total, "started": self.started, "updated": self.written}
        tmp = self.path.with_name(f"progress.json.tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)   # progress is a display: never a reason to stop the work


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

    def owns_folder(self) -> bool:
        """False for a copy of the review (a proposal, a sandbox) that shares the folder of the review it was copied
        from: it never saves over it. A copy given a folder of its own (an evaluation, a preview) saves there."""
        return Path(self.run_dir).resolve() not in self.store.copied_from

    def save(self):
        # always save into THIS context's run_dir (eval copies must never overwrite the main store)
        if not self.owns_folder():
            return
        from .config_lock import record_if_new
        record_if_new(self.cfg, self.store)   # the first save fixes the review-defining settings
        self.store.save(self.run_dir / "store.json")
        (self.run_dir / "cost.json").write_text(json.dumps(self.hub.tracker.summary(), indent=1))

    SAVE_EVERY = 24      # items per chunk at least: a chunk's slowest call holds up the next chunk
    SAVE_SECONDS = 90    # a save after a chunk when this long has passed since the last one: a stopped job loses at
                         # most this much work, and a large review (a save of 250 MB takes ~10 s) is not slowed down

    def map(self, fn, items, label: str | None = None):
        """Parallel map over items, in chunks, saved after a chunk when SAVE_SECONDS have passed, so finished work is
        never lost if the job stops or a call fails midway (e.g. quota); run again, it continues where it stopped.
        Progress (`label`: done of total) is written to progress.json for the interface while it runs.
        An item the model gives no usable reply for is skipped and reported, so one bad reply does not stop the stage;
        running the stage again retries it. Returns the results of the items that worked. Anything else (budget, quota,
        a network or account problem) still stops the job."""
        items = list(items)
        if not items:
            return []
        out, skipped = [], []
        step = max(self.SAVE_EVERY, 4 * self.writer.c.get("concurrency", 4))
        progress = Progress(self, label, len(items)) if self.owns_folder() else None
        last_save = time.time()

        def one(item):
            try:
                return fn(item)
            except (UnusableReply, Refused) as e:
                skipped.append(item)
                self.log(f"[skipped] {item}: {e}")
                return skipped          # the marker of a skipped item: never a result
            finally:
                if progress:
                    progress.tick()

        try:
            for i in range(0, len(items), step):
                out += self.writer.map(one, items[i:i + step])
                if i + step < len(items) and time.time() - last_save >= self.SAVE_SECONDS:
                    self.save()
                    last_save = time.time()
            return [r for r in out if r is not skipped]
        finally:
            if progress:
                progress.write()
            self.save()
            if skipped:
                self.log(f"[skipped] {len(skipped)} of {len(items)} item(s) had no usable model reply ({', '.join(map(str, skipped[:20]))}); "
                         "they are left to do: run this stage again to retry them (replies kept in bad_replies.jsonl)")

    BATCH_SIZE = 1000   # requests per batch: their answers are written to the review before the next batch is sent

    def map_batched(self, stage: str, prompt_of, fn, items, schema: dict | None = None):
        """ctx.map for a stage in pipeline.batch_stages on a model with a batch API: the prompts (prompt_of(item) gives
        (messages, tag), or None for an item that needs no call) are first answered at half price, then fn runs as
        always and finds its answers in the cache. Otherwise this is ctx.map."""
        items = list(items)
        llm = self.hub.model(stage)
        if stage not in (self.cfg["pipeline"].get("batch_stages") or []) or not llm.can_batch:
            return self.map(fn, items)
        pending, out = self.run_dir / f"pending_batch_{stage}.json", []
        for i in range(0, len(items), self.BATCH_SIZE):
            part = items[i:i + self.BATCH_SIZE]
            llm.batch_prefill([p for p in map(prompt_of, part) if p], pending, self.log, schema)
            out += self.map(fn, part)
        return out

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

"""The one list of stages (docs/refactor/04-refactor-plan.md §1.1 and §2).

Every other list of stages is derived from STAGES: the pipeline order and `--until` choices (pipeline.py), the
models per stage and their labels (config.LLM_STAGES / STAGE_LABELS / stage_of_tag), the stages whose model is fixed
once they have started (config_lock.STAGE_STARTED), the To-do groups (todos.STEPS), the Overview rows (ui/steps.py)
and the stage list of the web interface (/api/state "registry").

Older keys are accepted wherever a person or an older review folder may still use them (LEGACY): a run file's
`stage_models` and `pipeline.skip_stages`, `sragent run --until`, and the stage models recorded in store.json when a
stage started. `fulltext` used to be one stage for retrieval and full-text screening; its model is the eligibility
stage's model, and `--until fulltext` runs through eligibility.

The model-call tags (`plan`, `screen_full_text`, ...) are not renamed: they are written in every usage.jsonl and
cost history. Each stage lists the tag prefixes that belong to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class StageSpec:
    key: str
    label: str
    llm: bool                                # has a model of its own (stage_models.<key>), fixed once it has started
    produces: tuple[str, ...]                # node types (or meta keys, in brackets) this stage writes
    tags: tuple[str, ...] = ()               # model-call tag prefixes attributed to this stage
    legacy: tuple[str, ...] = ()             # earlier keys of this stage
    started: Callable = field(default=lambda st: False, compare=False, repr=False)

    @property
    def order(self) -> int:
        return KEYS.index(self.key) + 1


def _screen_at(st, *stages) -> bool:
    return any(n["data"].get("stage") in stages for n in st.by_type("screen"))


STAGES: tuple[StageSpec, ...] = (
    StageSpec("protocol", "Protocol", True, ("protocol", "criterion"), tags=("plan",), legacy=("plan",),
              started=lambda st: st.get("protocol") is not None),
    StageSpec("search", "Search", True, ("search", "record"), tags=("search",),
              started=lambda st: st.get("search") is not None),
    StageSpec("screen", "Title/abstract screening", True, ("screen",), tags=("screen_title_abstract",),
              started=lambda st: bool(st.by_type("screen"))),
    StageSpec("retrieve", "Full texts", False, ("fulltext",),
              started=lambda st: bool(st.by_type("fulltext")) or _screen_at(st, "retrieval")),
    StageSpec("eligibility", "Full-text screening", True, ("screen",), tags=("screen_full_text",), legacy=("fulltext",),
              started=lambda st: _screen_at(st, "full_text")),
    StageSpec("snowball", "Citation chasing", True, ("[snowball]", "record"),
              started=lambda st: st.get("snowball") is not None),
    StageSpec("extract", "Data extraction", True, ("extraction",), tags=("extract",),
              started=lambda st: bool(st.by_type("extraction"))),
    StageSpec("rob", "Risk of bias", True, ("rob",), tags=("rob",),
              started=lambda st: bool(st.by_type("rob"))),
    StageSpec("synthesis", "Synthesis", True, ("synthesis",), tags=("synthesis",), legacy=("synthesize",),
              started=lambda st: bool(st.by_type("synthesis"))),
    StageSpec("report", "Report", False, ("report",),
              started=lambda st: st.get("report") is not None),
)
KEYS: tuple[str, ...] = tuple(s.key for s in STAGES)
BY_KEY: dict[str, StageSpec] = {s.key: s for s in STAGES}
LEGACY: dict[str, str] = {old: s.key for s in STAGES for old in s.legacy}

# Revisions of dependent outputs, feedback and updates after a person's decisions have their own model slot until
# models are chosen per (stage, role) (04 §6, phase 5). It is not a stage: nothing in the pipeline order belongs to it.
FEEDBACK = "feedback"
FEEDBACK_LABEL = "Feedback, revisions and updates"


def canonical(key: str | None) -> str | None:
    """A stage key as used now (an earlier key is translated; anything else is returned as given)."""
    return LEGACY.get(key, key) if key else key


def label(key: str) -> str:
    k = canonical(key)
    return FEEDBACK_LABEL if k == FEEDBACK else BY_KEY[k].label


def model_slots() -> tuple[str, ...]:
    """The keys of stage_models: every stage with a model, then the feedback slot."""
    return tuple(s.key for s in STAGES if s.llm) + (FEEDBACK,)


def stage_of_tag(tag: str | None) -> str:
    """The stage a model call belongs to, from its tag; calls of no stage (feedback, revisions, checks) -> feedback."""
    t = tag or ""
    for s in STAGES:
        for prefix in s.tags:
            if t == prefix or t.startswith(prefix + "_"):
                return s.key
    return FEEDBACK


def canonical_keys(d: dict | None, what: str) -> dict:
    """A mapping keyed by stage (stage_models, recorded locks) with earlier keys translated. Both an earlier and the
    current key for the same stage is an error: which one is meant is not clear."""
    out: dict = {}
    for k, v in (d or {}).items():
        c = canonical(k)
        if c in out:
            old = ", ".join(o for o, n in LEGACY.items() if n == c)
            raise ValueError(f"{what}: {c!r} is set twice ({old} is its earlier name); keep one")
        out[c] = v
    return out


def run_until(key: str) -> str:
    """`--until` / `skip_stages` value: an earlier key is accepted."""
    k = canonical(key)
    if k not in BY_KEY:
        raise ValueError(f"unknown stage {key!r}; stages are {', '.join(KEYS)}")
    return k

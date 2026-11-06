"""The one list of stages (docs/refactor/04-refactor-plan.md §1.1 and §2).

Every other list of stages is derived from STAGES: the pipeline order and `--until` choices (pipeline.py), the
model cells per stage and role (config.MODEL_CELLS, the run file's `models:`), the stages whose models are fixed once
they have started (config_lock.STAGE_STARTED), the To-do groups (todos.STEPS), the Overview rows (ui/steps.py) and
the stage list of the web interface (/api/state "registry").

Roles (04 §1.2): the writer produces a stage's output; the verifier gives a second reading of the items that have a
reason (undecided, flagged, doubted). Each stage lists the roles it has. Model-call tags (`plan`, `screen_full_text`,
...) name what a call does; each stage lists the tag prefixes that belong to it.
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
    roles: tuple[str, ...] = ("writer",)     # the model roles of this stage (a stage with llm=False has none)
    started: Callable = field(default=lambda st: False, compare=False, repr=False)

    @property
    def order(self) -> int:
        return KEYS.index(self.key) + 1


def _retrieval_started(st) -> bool:
    from .selection import screened
    return bool(st.by_type("fulltext")) or any(v.get("stage") == "retrieval" for _, v in screened(st))


WV = ("writer", "verifier")
STAGES: tuple[StageSpec, ...] = (
    StageSpec("protocol", "Protocol", True, ("protocol", "criterion"), tags=("plan",),
              started=lambda st: st.get("protocol") is not None),
    StageSpec("search", "Search", True, ("search", "record"), tags=("search",),
              started=lambda st: st.get("search") is not None),
    StageSpec("screen", "Title/abstract screening", True, ("screen",), tags=("screen_title_abstract",), roles=WV,
              started=lambda st: bool(st.by_type("screen"))),
    StageSpec("retrieve", "Full texts", False, ("fulltext",),
              started=_retrieval_started),
    StageSpec("eligibility", "Full-text screening", True, ("eligibility",), tags=("screen_full_text", "eligibility"), roles=WV,
              started=lambda st: bool(st.by_type("eligibility"))),
    StageSpec("snowball", "Citation chasing", True, ("[snowball]", "record"),
              started=lambda st: st.get("snowball") is not None),
    StageSpec("extract", "Data extraction", True, ("extraction",), tags=("extract",),
              started=lambda st: bool(st.by_type("extraction"))),
    StageSpec("rob", "Risk of bias", True, ("rob",), tags=("rob",), roles=WV,
              started=lambda st: bool(st.by_type("rob"))),
    StageSpec("synthesis", "Synthesis", True, ("synthesis",), tags=("synthesis",), roles=WV,
              started=lambda st: bool(st.by_type("synthesis"))),
    StageSpec("report", "Report", False, ("report",),
              started=lambda st: st.get("report") is not None),
)
KEYS: tuple[str, ...] = tuple(s.key for s in STAGES)
BY_KEY: dict[str, StageSpec] = {s.key: s for s in STAGES}

# Your feedback and the agent's answer to it (locating what it concerns, checking it against the evidence, revising
# what it reaches) have model cells of their own. It is not a stage: nothing in the pipeline order belongs to it.
FEEDBACK = "feedback"
FEEDBACK_LABEL = "Feedback and revisions"
ROLES = WV


def label(key: str) -> str:
    return FEEDBACK_LABEL if key == FEEDBACK else BY_KEY[key].label


def model_slots() -> tuple[str, ...]:
    """The stages with model cells (`models.<stage>.<role>`): every stage with a model, then feedback."""
    return tuple(s.key for s in STAGES if s.llm) + (FEEDBACK,)


def roles_of(key: str) -> tuple[str, ...]:
    return WV if key == FEEDBACK else BY_KEY[key].roles


def stage_of_tag(tag: str | None) -> str:
    """The stage a model call belongs to, from its tag; calls of no stage (feedback, revisions, checks) -> feedback."""
    t = tag or ""
    for s in STAGES:
        for prefix in s.tags:
            if t == prefix or t.startswith(prefix + "_"):
                return s.key
    return FEEDBACK


def run_until(key: str) -> str:
    """A stage key given by a person (`--until`, `skip_stages`, a stage page), checked."""
    if key not in BY_KEY:
        raise ValueError(f"unknown stage {key!r}; stages are {', '.join(KEYS)}")
    return key

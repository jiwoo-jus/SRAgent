"""The JSON shape of the replies to the prompts that run once per record or study (prompts.py describes each in words).

Sent as a structured-output format on models that support it (Anthropic: output_config.format), so the reply is valid
JSON of this shape by construction. The API requires additionalProperties: false on every object, so a prompt whose reply
holds a free-form object ("the full revised output") has no schema and stays in plain JSON mode.
"""
from __future__ import annotations

STR, BOOL, NUM = {"type": "string"}, {"type": "boolean"}, {"type": "number"}


def _obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _list(item: dict) -> dict:
    return {"type": "array", "items": item}


def _enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


_EVIDENCE = _list(STR)
_ASSESSMENT = _enum("met", "not_met", "unclear")
_DECISION = _enum("include", "exclude", "uncertain")
_JUDGMENT = _enum("low", "some_concerns", "high", "unclear")

SCREEN = _obj(criteria=_list(_obj(id=STR, assessment=_ASSESSMENT, reason=STR, reported=BOOL, evidence=_EVIDENCE)),
              decision=_DECISION, reason=STR, confidence=NUM)
SCREEN_VERIFY = _obj(decision=_DECISION, agree=BOOL, explanation=STR,
                     differs_on=_list(_obj(id=STR, assessment=_ASSESSMENT, reason=STR, evidence=_EVIDENCE)))
SCREEN_VERIFY_BLIND = _obj(decision=_DECISION, explanation=STR,
                           assessments=_list(_obj(id=STR, assessment=_ASSESSMENT, reason=STR, evidence=_EVIDENCE)))
ELIGIBILITY_VERIFY = _obj(verdict=_enum("exclude", "include"), explanation=STR, evidence=_EVIDENCE)
ROB = _obj(domains=_list(_obj(domain=STR, judgment=_JUDGMENT, rationale=STR, evidence=_EVIDENCE)), overall=_JUDGMENT,
           doubts=_list(_obj(domain=STR, reason=STR)))
ROB_VERIFY = _obj(decided=BOOL, overall=_JUDGMENT, explanation=STR,
                  domains=_list(_obj(domain=STR, judgment=_JUDGMENT, agree=BOOL, rationale=STR, evidence=_EVIDENCE)))
SYNTH_VERIFY = _obj(support=_enum("supported", "partially_supported", "unsupported"), issues=_list(STR), overgeneralization=BOOL)


def extract(fields: list[dict]) -> dict:
    """The reply to EXTRACT for this review's data items (extraction_fields)."""
    item = _obj(value=STR, evidence=_EVIDENCE, entities=_list(STR), note=STR)
    return _obj(fields=_obj(**{f["name"]: item for f in fields}),
                eligibility_doubt={"anyOf": [_obj(criterion=STR, why=STR), {"type": "null"}]})

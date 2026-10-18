"""Controlled error injection with known corrections (gold by construction).

Error types (proposal: "incorrect eligibility decisions, inaccurate extracted findings,
unsupported risk-of-bias judgments, and overgeneralized synthesis statements"):

  extraction   a number in an extracted value is altered (quote left untouched)
  eligibility  an included study is marked excluded on criterion I3 (quote left untouched)
  rob          a 'low' domain judgment becomes 'high' with the original rationale/quote
  synthesis    a statement is rewritten (LLM) to drop a population/regimen qualifier

Each planted error = {"id", "type", "location", "node", "field", "gold", "corrupted", "description"}.
"""
from __future__ import annotations

import copy
import random
import re

from .. import prompts
from ..context import Ctx
from ..stages import screening

NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w])")
NUMERIC_FIELDS = ["sample_size", "pk_results", "efficacy_safety", "exposure_response", "intervention"]


def alter_number(value: str, rnd: random.Random) -> str | None:
    nums = [m for m in NUM.finditer(value) if float(m.group(1)) not in (0.0,)]
    if not nums:
        return None
    m = rnd.choice(nums[:4])
    x = float(m.group(1))
    if "." in m.group(1):
        dec = len(m.group(1).split(".")[1])
        new = f"{x * rnd.choice([1.6, 0.55, 2.2]):.{dec}f}"
    else:
        new = str(int(round(x * rnd.choice([2, 3])) + (1 if x < 3 else 0)))
    if new == m.group(1):
        return None
    return value[:m.start()] + new + value[m.end():]


def inject_extraction(ctx: Ctx, rid: str, rnd: random.Random, used_fields=()) -> dict | None:
    n = ctx.store.get(f"ext:{rid}")
    if not n:
        return None
    for f in rnd.sample(NUMERIC_FIELDS, len(NUMERIC_FIELDS)):
        if f in used_fields or f not in n["data"]["fields"]:
            continue
        v = n["data"]["fields"][f]
        if v["value"].upper().startswith("NR"):
            continue
        new = alter_number(v["value"], rnd)
        if new:
            data = copy.deepcopy(n["data"])
            data["fields"][f]["value"] = new
            ctx.store.upsert(n["id"], "extraction", data, depends_on=n["depends_on"], evidence=n["evidence"],
                             actor="injector", reason="planted error")
            return {"type": "extraction", "node": n["id"], "field": f, "location": f"{n['id']} {f}",
                    "gold": v["value"], "corrupted": new,
                    "description": f"A number in '{f}' of study {rid} was altered and no longer matches the paper."}
    return None


def inject_eligibility(ctx: Ctx, rid: str, structural: bool = True) -> dict | None:
    st = ctx.store
    n = st.get(f"screen:{rid}")
    if not n or n["data"]["decision"] != "include":
        return None
    data = copy.deepcopy(n["data"])
    for a in data["criteria"]:
        if a["id"] == "I3":
            a["assessment"] = "not_met"
    p = st.get("protocol")["data"]
    dec, because = screening.rule_decision(p, data["criteria"], data["stage"])
    data["decision"], data["because"] = dec, because
    gold = copy.deepcopy(n["data"])
    st.upsert(n["id"], "screen", data, depends_on=n["depends_on"], actor="injector", reason="planted error")
    if structural:
        for kind in ("ext", "rob"):
            if st.get(f"{kind}:{rid}"):
                st.set_status(f"{kind}:{rid}", "excluded", actor="injector", reason="planted error")
    return {"type": "eligibility", "node": n["id"], "field": "I3", "location": n["id"],
            "gold": {"decision": gold["decision"], "I3": "met"}, "corrupted": {"decision": dec, "I3": "not_met"},
            "description": f"Study {rid} meets all criteria but was marked excluded (I3 not met)."}


def inject_rob(ctx: Ctx, rid: str, rnd: random.Random) -> dict | None:
    n = ctx.store.get(f"rob:{rid}")
    if not n:
        return None
    lows = [i for i, d in enumerate(n["data"]["domains"]) if d.get("judgment") == "low"]
    if not lows:
        return None
    i = rnd.choice(lows)
    data = copy.deepcopy(n["data"])
    dom = data["domains"][i]
    dom["judgment"] = "high"
    if data.get("overall") in ("low", "some_concerns", "unclear"):
        data["overall"] = "high"
    ctx.store.upsert(n["id"], "rob", data, depends_on=n["depends_on"], evidence=n["evidence"],
                     actor="injector", reason="planted error")
    return {"type": "risk_of_bias", "node": n["id"], "field": dom.get("domain"),
            "location": f"{n['id']} {dom.get('domain')}", "gold": "low", "corrupted": "high",
            "description": f"Domain '{dom.get('domain')}' of study {rid} judged high risk although the "
                           f"rationale/quote support low risk."}


def inject_synthesis(ctx: Ctx, sid: str) -> dict | None:
    n = ctx.store.get(sid)
    if not n:
        return None
    out = ctx.agent.chat(ctx.msgs(prompts.INJECT_OVERGEN.format(text=n["data"]["text"])), tag="inject")
    new = out.get("text")
    if not new or new.strip() == n["data"]["text"].strip():
        return None
    data = copy.deepcopy(n["data"])
    gold = data["text"]
    data["text"] = new
    data.pop("verification", None)
    ctx.store.upsert(sid, "synthesis", data, depends_on=n["depends_on"], actor="injector", reason="planted error")
    return {"type": "synthesis", "node": sid, "field": "text", "location": sid.split(":", 1)[1],
            "gold": gold, "corrupted": new,
            "description": f"Statement {sid} over-generalised: {out.get('what_changed', '')}"}

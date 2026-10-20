"""Stage 1: review planning -> protocol (PICO, atomic eligibility criteria, key concepts).

A human checkpoint: the protocol is written to <run_dir>/protocol.yaml. Edit it and the next
`sragent run` uses your edited version (it is never silently overwritten).
"""
from __future__ import annotations

import json

import yaml

from .. import prompts, terms
from ..context import Ctx


def plan(ctx: Ctx) -> dict:
    pfile = ctx.run_dir / "protocol.yaml"
    given = ctx.topic.get("protocol_file")
    if given:
        protocol = yaml.safe_load(open(given))
        ctx.log(f"[plan] using protocol from {given}")
    elif pfile.exists():
        protocol = yaml.safe_load(open(pfile))
        ctx.log(f"[plan] using existing (possibly human-edited) {pfile}")
        stored = (ctx.store.get("protocol") or {}).get("data")
        if stored is not None and _differs(stored, protocol):
            # an edited protocol.yaml is a person's judgment: recorded, and refused in the agent-only condition
            from .. import provenance
            provenance.require_judgment_allowed(ctx, "editing protocol.yaml")
            provenance.record(ctx, "human_judgment", "protocol_edited", changed=_changed_criteria(stored, protocol))
            ctx.log("[plan] protocol.yaml was edited by a person; the edit is recorded in provenance.jsonl")
    else:
        seed = ctx.topic.get("seed_eligibility", "")
        seed_txt = f"SEED ELIGIBILITY TEXT (from the review team; stay faithful to it):\n{seed}" if seed else ""
        protocol = ctx.agent.chat(ctx.msgs(prompts.PLAN.format(question=ctx.topic["question"], seed=seed_txt)),
                                  tag="plan")
        ctx.log(f"[plan] drafted protocol with {len(protocol.get('criteria', []))} criteria")

    # validate key concepts in external databases (synonyms feed the search strategy)
    if ctx.cfg["pipeline"].get("validate_terms", True):
        for kc in protocol.get("key_concepts", []):
            if kc.get("kind") in ("drug", "disease"):
                v = terms.validate(kc["term"], kc["kind"])
                kc["validation"] = {k: v[k] for k in ("status", "preferred", "ids", "synonyms")}
                ctx.log(f"[plan] concept '{kc['term']}' -> {v['status']} {v['ids']}")
    with open(pfile, "w") as f:
        yaml.safe_dump(protocol, f, sort_keys=False, allow_unicode=True)

    st = ctx.store
    old = st.get("protocol")
    if old is None or old["data"] != protocol:
        deps = [d for d in ("cfg:question", "cfg:seed_eligibility") if st.get(d)]
        st.upsert("protocol", "protocol", protocol, depends_on=deps, reason="planned" if old is None else "protocol updated")
    sync_criteria(st, protocol)
    return protocol


def sync_criteria(st, protocol: dict, actor: str = "agent", reason: str = "protocol criterion") -> dict:
    """One `crit:` node per criterion in the protocol: new ones created, changed ones versioned, and
    criteria no longer in the protocol retired (not left as live inputs). Returns what changed."""
    out = {"added": [], "changed": [], "retired": []}
    ids = set()
    for c in protocol.get("criteria", []):
        cid = f"crit:{c['id']}"
        ids.add(cid)
        old = st.get(cid)
        if old is None:
            st.upsert(cid, "criterion", c, depends_on=["protocol"], actor=actor, reason=reason)
            out["added"].append(c["id"])
        elif old["data"] != c or old["status"] != "active":
            st.upsert(cid, "criterion", c, depends_on=["protocol"], actor=actor, reason=reason)
            out["changed"].append(c["id"])
        else:
            st.touch(cid)          # unchanged by this protocol edit: current with the new protocol version
    for n in st.by_type("criterion"):
        if n["id"] not in ids and n["status"] == "active":
            st.set_status(n["id"], "retired", actor=actor, reason="removed from the protocol")
            out["retired"].append(n["id"].split(":", 1)[1])
    return out


def _differs(a: dict, b: dict) -> bool:
    keep = lambda p: {k: v for k, v in p.items() if k != "key_concepts"}   # concept validation is added by the run
    return json.dumps(keep(a), sort_keys=True, default=str) != json.dumps(keep(b), sort_keys=True, default=str)


def _changed_criteria(a: dict, b: dict) -> dict:
    ca = {c["id"]: c for c in a.get("criteria", [])}
    cb = {c["id"]: c for c in b.get("criteria", [])}
    return {"added": sorted(set(cb) - set(ca)), "removed": sorted(set(ca) - set(cb)),
            "changed": sorted(k for k in ca if k in cb and ca[k] != cb[k]),
            "question": a.get("question") != b.get("question")}


def criteria_text(protocol: dict, stage: str | None = None) -> str:
    lines = []
    for c in protocol.get("criteria", []):
        if c.get("type") == "practical" and stage is not None:
            continue  # access/language requirements are checked by humans from metadata
        if stage == "title_abstract" and c.get("stage") == "full_text":
            continue
        grp = f", any of group {c['any_of']}" if c.get("any_of") else ""
        lines.append(f"- {c['id']} ({c['type']}{grp}): {c['text']}")
    return "\n".join(lines)


def synonyms_text(protocol: dict) -> str:
    """Database-validated synonyms of key concepts (e.g. ACE910 = emicizumab) for the screener."""
    out = []
    for kc in protocol.get("key_concepts", []):
        v = kc.get("validation") or {}
        if v.get("synonyms"):
            out.append(f"- {kc['term']} ({', '.join(f'{k}:{x}' for k, x in (v.get('ids') or {}).items())}) "
                       f"also known as: {', '.join(v['synonyms'][:15])}")
    if not out:
        return ""
    return "KNOWN SYNONYMS (validated in RxNorm/ChEMBL/PubChem/MONDO):\n" + "\n".join(out) + "\n\n"


def protocol_brief(protocol: dict) -> str:
    return json.dumps({k: protocol.get(k) for k in ("question", "pico")}, indent=1)

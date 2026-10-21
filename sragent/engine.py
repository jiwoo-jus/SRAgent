"""One update engine for everything downstream of a change.

Whatever changed (a person's decision, a corrected value, an edited protocol, an accepted settings change,
a new full text), the consequences are computed the same way:

  plan(ctx)      the stale nodes (graph.stale: a dependency moved on since the node was computed) and the
                 missing outputs (an included study without extraction, a study the synthesis never saw),
                 each with the action that brings it up to date and an estimated cost. No model call.
  execute(ctx)   runs the plan in dependency order, in rounds, until nothing is stale: screening is
                 re-assessed only on the criteria that changed; an output a person decided, confirmed or
                 edited is kept (and reported), never overwritten; the synthesis is revised statement by
                 statement; the report is rendered last.

The same function serves "Update now" and the next `sragent run` (on the live store: changes are applied)
and feedback or a manual edit (on a sandbox store: changes become a proposal). `execute` records every
change it makes in a change set so the caller can show, propose or log it.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict

from . import graph
from .context import Ctx
from .store import study_of

MAX_ROUNDS = 4
ORDER = ["rules", "screen", "status", "extraction", "rob", "synthesis", "integrate", "report"]
STAGE_FOR = {"reassess": "screen", "rescreen": "fulltext", "extract": "extract", "assess": "rob",
             "revise": "feedback", "integrate": "feedback", "retire": None, "rederive": None, "keep": None,
             "render": None, "rules": None}


# ---------------------------------------------------------------- the plan

def _changed_criteria(st, node: dict, protocol: dict) -> list[str]:
    """Criteria added or changed since this screen node last saw the protocol (from the protocol's history)."""
    rec = (node.get("dep_versions") or {}).get("protocol")
    p = st.get("protocol")
    if not p or rec is None or rec >= p["version"]:
        return []
    old = next((h["data"] for h in p["history"] if h["version"] == rec), None)
    if old is None:
        return [c["id"] for c in protocol.get("criteria", []) if c.get("type") != "practical"]
    before = {c["id"]: c for c in old.get("criteria", [])}
    out = []
    for c in protocol.get("criteria", []):
        if c.get("type") == "practical":
            continue
        b = before.get(c["id"])
        if b is None or any(b.get(k) != c.get(k) for k in ("type", "text", "stage", "any_of")):
            out.append(c["id"])
    return out


def plan(ctx: Ctx, roots: set[str] | None = None) -> dict:
    """What is out of date and what to do about it. `roots`: only consequences of these nodes (a feedback
    proposal must not pick up unrelated stale work)."""
    st = ctx.store
    protocol = (st.get("protocol") or {}).get("data") or {}
    inc = set(st.included_ids())
    scope = None
    if roots:
        scope = set(roots)
        for r in roots:
            scope.update(st.descendants(r))
    items = []
    stale = graph.stale(st)
    for nid, reasons in stale.items():
        n = st.get(nid)
        if scope is not None and not ({r["dep"] for r in reasons} & scope) and nid not in scope:
            continue
        deps = {r["dep"] for r in reasons}
        why = [f"{r['dep']} {r['why']}" for r in reasons]
        pinned = graph.human_pinned(n)
        t = n["type"]
        rid = study_of(nid)
        if t == "screen":
            if n["data"].get("rule"):
                items.append(_item(nid, t, "rederive", why, False, rid))      # rule exclusions follow the rules step
            elif any(d.startswith("ft:") for d in deps) and not pinned:
                items.append(_item(nid, t, "rescreen", why, False, rid))
            else:
                crits = _changed_criteria(st, n, protocol) if ("protocol" in deps or any(d.startswith("crit:") for d in deps)) else []
                stage = "title_abstract" if n["data"]["stage"] in ("title_abstract", "retrieval") else "full_text"
                crits = [c for c in crits if not (stage == "title_abstract" and _crit(protocol, c).get("stage") == "full_text")]
                if pinned:   # a person decided: the new criteria are assessed and shown beside the decision, which is kept
                    items.append(_item(nid, t, "reassess" if crits else "touch", why, True, rid, criteria=crits, stage=stage))
                else:
                    items.append(_item(nid, t, "reassess" if crits else "rederive", why, False, rid, criteria=crits, stage=stage))
        elif t == "extraction":
            if rid not in inc:
                items.append(_item(nid, t, "retire", why, pinned, rid))
            elif deps <= {f"screen:{rid}"}:
                items.append(_item(nid, t, "touch", why, pinned, rid))       # the decision is still include: nothing to redo
            else:
                items.append(_item(nid, t, "extract", why, pinned, rid))     # a person's edited values are kept
        elif t == "rob":
            if rid not in inc:
                items.append(_item(nid, t, "retire", why, pinned, rid))
            elif deps <= {f"screen:{rid}"}:
                items.append(_item(nid, t, "touch", why, pinned, rid))
            elif pinned:
                items.append(_item(nid, t, "keep", why, True, rid))          # confirmed by a person: flagged, kept
            else:
                items.append(_item(nid, t, "assess", why, False, rid))
        elif t == "synthesis":
            if pinned:
                items.append(_item(nid, t, "keep", why, True, rid))
            else:
                items.append(_item(nid, t, "revise", why, False, rid, sources=sorted(deps)))
        elif t == "report":
            items.append(_item(nid, t, "render", why, False, None))
        elif t in ("protocol", "search", "criterion"):
            items.append(_item(nid, t, "keep", why, False, None, manual=True))   # re-planning is a person's call
    # outputs that are missing (not stale: they do not exist yet)
    has_syn = bool(st.by_type("synthesis", active_only=True))
    considered = set(st.meta.get("synthesis_considered") or [])
    for rid in sorted(inc):
        if scope is not None and f"screen:{rid}" not in scope:
            continue
        back = False
        if not st.get(f"ext:{rid}") and f"ext:{rid}" not in {i["node"] for i in items}:
            items.append(_item(f"ext:{rid}", "extraction", "extract", ["included, not extracted"], False, rid))
        elif st.get(f"ext:{rid}") and st.get(f"ext:{rid}")["status"] != "active" and \
                f"ext:{rid}" not in {i["node"] for i in items}:
            items.append(_item(f"ext:{rid}", "extraction", "reactivate", ["included again"], False, rid))
            back = True   # a study included again: the statements that cited it must get it back (history)
        if st.get(f"ext:{rid}") and not st.get(f"rob:{rid}") and f"rob:{rid}" not in {i["node"] for i in items}:
            items.append(_item(f"rob:{rid}", "rob", "assess", ["included, not assessed"], False, rid))
        if has_syn and (rid not in considered or back):
            items.append(_item(f"syn:+{rid}", "synthesis", "integrate",
                               ["included again, dropped from the synthesis" if back else "included, not in the synthesis"], False, rid))
    # active outputs of studies that are no longer included
    for n in st.by_type("extraction", active_only=True) + st.by_type("rob", active_only=True):
        rid = study_of(n["id"])
        if rid not in inc and n["id"] not in {i["node"] for i in items} and (scope is None or f"screen:{rid}" in scope):
            items.append(_item(n["id"], n["type"], "retire", ["study no longer included"], graph.human_pinned(n), rid))
    for n in st.by_type("synthesis", active_only=True):
        gone = [c for c in n["data"].get("cites", []) if c not in inc]
        if gone and n["id"] not in {i["node"] for i in items} and (scope is None or any(f"screen:{c}" in scope for c in gone)):
            items.append(_item(n["id"], "synthesis", "revise", [f"cites excluded {', '.join(gone)}"], graph.human_pinned(n),
                               None, sources=[f"screen:{c}" for c in gone]))
    return {"items": items, "cost": _cost(ctx, items),
            "summary": _summary(items)}


def _crit(protocol: dict, cid: str) -> dict:
    return next((c for c in protocol.get("criteria", []) if c["id"] == cid), {})


def _item(node, ntype, action, why, pinned, rid, **extra) -> dict:
    return {"node": node, "type": ntype, "action": action, "why": why, "pinned": pinned, "rid": rid, **extra}


def _summary(items: list[dict]) -> dict:
    c = defaultdict(int)
    for i in items:
        c[f"{i['type']}:{i['action']}"] += 1
    return dict(sorted(c.items()))


def _cost(ctx: Ctx, items: list[dict]) -> dict:
    from .estimate import estimate_stages
    calls = defaultdict(int)
    for i in items:
        a = i["action"]
        if a == "reassess":
            calls["screen_title_abstract" if i.get("stage") == "title_abstract" else "screen_full_text"] += 1
        elif a == "rescreen":
            calls["screen_full_text"] += 1
        elif a == "extract":
            calls["extract"] += 1
        elif a == "assess":
            calls["rob"] += 1
        elif a in ("revise", "integrate"):
            calls["feedback"] += 1
            calls["synthesis_verify"] += 1
    try:
        e = estimate_stages(dict(calls), ctx.cfg)
    except Exception:
        return {"total_usd": None, "rows": []}
    return {"total_usd": e["total_usd"], "range_usd": e["range_usd"], "rows": e["rows"]}


# ---------------------------------------------------------------- execution

def execute(ctx: Ctx, cs: dict | None = None, roots: set[str] | None = None, tag: str = "update",
            render: bool = True, max_rounds: int = MAX_ROUNDS, policy: bool = True) -> dict:
    """Run the plan until nothing in scope is stale. Every change goes into `cs["changes"]` (before/after)
    and `cs["kept"]` lists what a person pinned. On a sandbox store this is a proposal; on the live store
    it is applied, except what `update_policy` says to propose (done in a sandbox and saved as a proposal,
    `cs["proposed"]`) or to leave to a person (`manual`). policy=False: the caller is already in a sandbox."""
    from . import rules
    from .stages import report
    cs = cs if cs is not None else {"id": tag, "changes": []}
    cs.setdefault("changes", [])
    cs.setdefault("kept", [])
    cs.setdefault("rounds", [])
    st = ctx.store
    protocol = (st.get("protocol") or {}).get("data") or {}
    pol = dict(ctx.cfg.get("update_policy") or {}) if policy else {}
    deferred: dict[str, dict] = {}
    for rnd in range(max_rounds):
        p = plan(ctx, roots)
        for i in p["items"]:    # the policy: what a person wants to see first is not applied
            if i["action"] in ("keep", "render", "touch"):
                continue
            mode = pol.get(i["type"], "auto")
            if mode == "propose":
                deferred.setdefault(i["node"], i)
                i["action"] = "keep"; i["manual"] = False; i["policy"] = "propose"
            elif mode == "manual":
                i["action"] = "keep"; i["manual"] = True; i["policy"] = "manual"
        todo = [i for i in p["items"] if i["action"] not in ("keep", "render")]
        kept = [i for i in p["items"] if i["action"] == "keep"]
        cs["rounds"].append({"round": rnd, "summary": p["summary"], "est_usd": p["cost"].get("total_usd")})
        for k in kept:
            if k["node"] not in {x["node"] for x in cs["kept"]}:
                cs["kept"].append({"node": k["node"], "why": k["why"], "pinned": k["pinned"], "manual": k.get("manual", False)})
        if not todo:
            break
        # 1. the screening rules (a changed rule set, a disabled rule): deterministic, first
        if any(i["type"] == "screen" for i in todo) and st.get("cfg:screening_rules"):
            rules.apply(ctx, protocol)
        # 2. screening: re-assess only the changed criteria; re-derive the rest
        for i in [x for x in todo if x["type"] == "screen"]:
            _screen(ctx, cs, i, protocol, tag)
        for i in [x for x in todo if x["action"] == "touch"]:
            st.touch(i["node"])
        # 3. statuses follow the included set (free)
        _statuses(ctx, cs)
        # 4. extraction, then risk of bias, for the stale and the missing
        inc = set(st.included_ids())
        for i in [x for x in todo if x["type"] == "extraction" and x["action"] in ("extract", "reactivate") and x["rid"] in inc]:
            _extract(ctx, cs, i)
        for i in [x for x in todo if x["type"] == "rob" and x["action"] == "assess" and x["rid"] in inc and st.get(f"ext:{x['rid']}")]:
            _rob(ctx, cs, i)
        # 5. the synthesis: revise the statements the changes reach, integrate new studies, check what changed
        _synthesis(ctx, cs, [x for x in todo if x["type"] == "synthesis"], tag)
    if deferred:
        cs["proposed"] = propose(ctx, sorted(deferred), tag)
    if render and st.get("protocol") and st.by_type("synthesis", active_only=True) and cs["changes"]:
        report.write_reports(ctx)
    return cs


def propose(ctx: Ctx, nodes: list[str], tag: str = "update") -> str | None:
    """Bring the given out-of-date nodes up to date in a sandbox and save the result as a proposal
    (a change set the To-do list offers to accept or reject). Returns the change set id."""
    import dataclasses
    import time
    from . import feedback
    t0, usd0 = time.time(), ctx.hub.tracker.spent
    sb = dataclasses.replace(ctx, store=ctx.store.clone())
    cs = {"id": f"UP{int(t0 * 1000) % 10**9}", "kind": "update", "label": "Update of out-of-date outputs",
          "feedback": f"Bring {len(nodes)} out-of-date output(s) up to date: {', '.join(nodes[:8])}{' …' if len(nodes) > 8 else ''}",
          "target": nodes[0] if len(nodes) == 1 else None, "verdict": "update", "changes": [], "status": "proposed",
          "created": t0, "mode": {"verify": False, "propagate": True}, "policy": "propose"}
    execute(sb, cs, roots=None, tag=tag, render=False, policy=False)
    cs["changes"] = [c for c in cs["changes"] if c["node"] in set(nodes) or c["node"].startswith("syn:")] or cs["changes"]
    cs["explanation"] = f"{len(cs['changes'])} output(s) would change (update_policy: propose)."
    cs["cost_usd"] = round(ctx.hub.tracker.spent - usd0, 5)
    cs["seconds"] = round(time.time() - t0, 1)
    if not cs["changes"]:
        cs["status"] = "no_change"
        feedback._save_changeset(ctx, cs)
        return None
    feedback.sandbox_delta(ctx.store, sb.store, cs)
    feedback.record_bases(ctx.store, cs)
    feedback._save_changeset(ctx, cs)
    for n in nodes[:1]:
        if ctx.store.get(n):
            ctx.store.flag(n, f"{cs['label']} {cs['id']} pending review", actor="agent", source="update")
    return cs["id"]


def preview(ctx: Ctx, node_id: str) -> dict:
    """What a change to this node would reach, before it is made: the plan on a copy where the node has
    a new version. No model call; the cost is an estimate."""
    import dataclasses
    st = ctx.store
    if not st.get(node_id):
        return {"node": node_id, "items": [], "summary": {}, "cost": {"total_usd": 0}, "reach": {}, "lines": []}
    sb = dataclasses.replace(ctx, store=st.clone())
    sb.store.nodes[node_id]["version"] += 1
    p = plan(sb, roots={node_id})
    items = [i for i in p["items"] if i["node"] != node_id]
    by_type = defaultdict(list)
    for i in items:
        by_type[i["type"]].append(i["node"])
    return {"node": node_id, "items": items, "summary": _summary(items), "cost": _cost(ctx, items),
            "reach": {t: sorted(v) for t, v in by_type.items()},
            "lines": describe({"items": items})[:20]}


def _record(cs: dict, node: str, before, after, action: str, role: str = "dependent", **extra) -> None:
    from .feedback import _changed
    if before is not None and after is not None and isinstance(before, dict) and isinstance(after, dict) and \
            not _changed(before, after, extra.get("ntype", "")) and extra.get("status_only") is None:
        return
    cs["changes"].append({"node": node, "role": role, "before": before, "after": after, "action": action,
                          **{k: v for k, v in extra.items() if k not in ("ntype", "status_only")}})


def _screen(ctx: Ctx, cs: dict, i: dict, protocol: dict, tag: str) -> None:
    from .stages import screening
    st = ctx.store
    n = st.get(i["node"])
    if not n:
        return
    before = copy.deepcopy(n["data"])
    rid = i["rid"]
    if n["data"].get("rule"):            # excluded by a screening rule: the rules step decided it; nothing to re-derive
        st.touch(i["node"])
        return
    if i["action"] == "reassess":
        screening.reassess(ctx, protocol, rid, i["criteria"], i["stage"], tag_suffix=f"_{tag}")
    elif i["action"] == "rescreen":
        screening.review_one(ctx, protocol, rid)
    else:   # rederive from the stored assessments (a removed criterion, a changed policy); never includes without a full text
        d = dict(n["data"])
        dec, because, stage, by_rule = screening.rederive(protocol, d, ctx.cfg["pipeline"].get("fulltext_unclear", "await"))
        if (dec, because, stage) != (d["decision"], d.get("because"), d["stage"]):
            d.update({"decision": dec, "because": because, "stage": stage})
            d.pop("unclear_by_rule", None)
            if by_rule:
                d["unclear_by_rule"] = by_rule
        st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], reason=f"{tag}: decision re-derived ({'; '.join(i['why'])[:160]})")
    after = st.get(i["node"])["data"]
    _record(cs, i["node"], before, after, i["action"], ntype="screen")
    screening.sync_awaiting_flags(ctx)


def _statuses(ctx: Ctx, cs: dict) -> None:
    st = ctx.store
    inc = set(st.included_ids())
    for n in st.by_type("extraction") + st.by_type("rob"):
        rid = study_of(n["id"])
        want = "active" if rid in inc else "excluded"
        if n["status"] in ("active", "excluded") and n["status"] != want:
            st.set_status(n["id"], want, reason="follows the eligibility decision")
            _record(cs, n["id"], {"status": "excluded" if want == "active" else "active"}, {"status": want},
                    "reactivate" if want == "active" else "retire", status_only=True)


def _extract(ctx: Ctx, cs: dict, i: dict) -> None:
    from .stages import extraction, fulltext
    st = ctx.store
    rid = i["rid"]
    if st.get(f"ft:{rid}") is None:
        fulltext.fetch_one(ctx, rid)
    old = st.get(f"ext:{rid}")
    before = copy.deepcopy(old["data"]) if old else None
    extraction.extract_one(ctx, rid)
    _record(cs, f"ext:{rid}", before, st.get(f"ext:{rid}")["data"], i["action"], ntype="extraction")


def _rob(ctx: Ctx, cs: dict, i: dict) -> None:
    from .stages import rob
    st = ctx.store
    rid = i["rid"]
    old = st.get(f"rob:{rid}")
    before = copy.deepcopy(old["data"]) if old else None
    rob.rob_one(ctx, rid)
    _record(cs, f"rob:{rid}", before, st.get(f"rob:{rid}")["data"], i["action"], ntype="rob")


def _synthesis(ctx: Ctx, cs: dict, items: list[dict], tag: str) -> None:
    from . import feedback
    from .stages import synthesis
    st = ctx.store
    if not st.by_type("synthesis", active_only=True):
        return
    inc = set(st.included_ids())
    touched = set()
    for i in [x for x in items if x["action"] == "revise"]:
        n = st.get(i["node"])
        if not n or n["status"] != "active":
            continue
        gone = [c for c in n["data"].get("cites", []) if c not in inc]
        if gone:   # a cited study left the review: deterministic first, then the wording
            for rid in gone:
                r = feedback._revise_dependent(ctx, cs, i["node"], f"screen:{rid}", {"included": True, "study": rid},
                                               {"included": False, "study": rid}, tp=tag)
                n2 = st.get(i["node"])
                if n2["status"] != "active":
                    break
                if r is None and rid in n2["data"].get("cites", []):     # the model did not drop it: do it ourselves
                    d = dict(n2["data"])
                    d["cites"] = [c for c in d["cites"] if c != rid]
                    feedback.apply_revised(ctx, i["node"], d, reason=f"{tag}: {rid} is no longer included")
                    _record(cs, i["node"], n2["data"], st.get(i["node"])["data"], "revise", ntype="synthesis")
            touched.add(i["node"])
            continue
        # an upstream output of a cited study changed: one revision per statement, with the change that
        # matters most (an extracted value before a risk-of-bias rating); the others are current after it
        srcs = [st.get(x) for x in (i.get("sources") or []) if st.get(x) and st.get(x)["type"] in ("extraction", "rob")]
        srcs.sort(key=lambda x: x["type"] != "extraction")
        if srcs:
            src = srcs[0]
            rec = (n.get("dep_versions") or {}).get(src["id"])
            old = next((h["data"] for h in src["history"] if h["version"] == rec), None) if rec else None
            feedback._revise_dependent(ctx, cs, i["node"], src["id"], old or {}, src["data"], tp=tag)
            if st.get(i["node"])["status"] == "active":
                st.touch(i["node"])
            touched.add(i["node"])
    for i in [x for x in items if x["action"] == "integrate"]:
        rid = i["rid"]
        if rid in inc and st.get(f"ext:{rid}") and st.get(f"ext:{rid}")["status"] == "active":
            before = {n["id"] for n in st.by_type("synthesis", active_only=True)}
            feedback._include_study(ctx, cs, rid, tp=tag, integrate=True)
            touched |= {c["node"] for c in cs["changes"] if c["node"].startswith("syn:")} - before | \
                {n["id"] for n in st.by_type("synthesis", active_only=True)} - before
    st.meta["synthesis_considered"] = sorted(inc)
    if ctx.cfg["pipeline"].get("verify_synthesis", True):
        for sid in sorted(touched):
            if st.get(sid) and st.get(sid)["status"] == "active":
                synthesis.verify_one(ctx, sid)
    # statements with no citation left after all that: retired (recorded)
    for n in st.by_type("synthesis", active_only=True):
        if n["data"].get("cites") == [] and any(h["data"].get("cites") for h in n["history"]):
            st.set_status(n["id"], synthesis.RETIRED, reason="no included study supports it any more")
            _record(cs, n["id"], {"status": "active"}, {"status": synthesis.RETIRED}, "retire", status_only=True)


def describe(p: dict) -> list[str]:
    """Plain lines for the To-do list and the log."""
    lines = []
    for i in p["items"]:
        if i["action"] == "keep":
            lines.append(f"{i['node']}: kept ({'decided or edited by you' if i['pinned'] else 'needs a person'}): {'; '.join(i['why'])[:120]}")
        elif i["action"] == "reassess":
            lines.append(f"{i['node']}: re-assess criteria {', '.join(i['criteria'])}")
        else:
            lines.append(f"{i['node']}: {i['action']} ({'; '.join(i['why'])[:120]})")
    return lines

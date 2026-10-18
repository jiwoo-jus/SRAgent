"""Command-line interface.

  sragent run       --config C [--until STAGE] [--plan-only] [--fresh]
  sragent ui        --config C [--port 8765]   (local web interface)
  sragent pdfs      --config C [--list]       (process PDFs put in <run_dir>/fulltext/<rid>.pdf)
  sragent sync      --config C                (update outputs after human decisions)
  sragent status    --config C
  sragent rescreen  --config C          (after editing protocol.yaml; no LLM calls)
  sragent feedback  --config C --text "..." [--target NODE] [--pmid PMID] [--local-edit] [--no-verify] [--accept]
  sragent accept    --config C --id FBxxxx
  sragent reject    --config C --id FBxxxx [--note "..."]
  sragent eval      --config C [--suite all|screening|grounding|claim1|claim2|judge] [--n-cases N]
  sragent data      --synergy KEY            (download + resolve a SYNERGY benchmark dataset)
  sragent runs                              (every run file under configs/ with its status)
  sragent local-endpoints --config C [--nodes N ... | --urls U ...] [--roles R ...] [--add] [--probe]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .context import Ctx


def _ctx(args, fresh=False):
    over = {}
    if getattr(args, "budget", None) is not None:
        over["budget_usd"] = args.budget
    if getattr(args, "run_dir", None):
        over["run_dir"] = args.run_dir
    cfg = load_config(args.config, over)
    return Ctx.create(cfg, fresh=fresh)


def cmd_run(args):
    from . import pipeline
    ctx = _ctx(args, fresh=args.fresh)
    pipeline.run(ctx, until=args.until, stop_after_plan=args.plan_only)
    print(json.dumps(ctx.hub.tracker.summary(), indent=1))


def cmd_rescreen(args):
    from .stages import screening
    import yaml
    ctx = _ctx(args)
    protocol = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    ctx.store.upsert("protocol", "protocol", protocol, actor="user", reason="protocol edited by user")
    for c in protocol.get("criteria", []):
        ctx.store.upsert(f"crit:{c['id']}", "criterion", c, depends_on=["protocol"], actor="user", reason="protocol edited")
    print(screening.rescreen_rule_only(ctx, protocol))


def cmd_ui(args):
    from .ui.server import serve
    serve(args.config, host=args.host, port=args.port, open_browser=not args.no_browser)


def cmd_pdfs(args):
    from . import sync
    from .stages.fulltext import fulltext_dir, write_needed_pdfs
    ctx = _ctx(args)
    if args.list:
        p = write_needed_pdfs(ctx)
        print(f"Put PDFs in {fulltext_dir(ctx)} named <rid>.pdf. Wanted files: {p}")
        print(p.read_text())
        return
    print(json.dumps(sync.process_pdfs(ctx), indent=1))


def cmd_sync(args):
    from . import sync
    ctx = _ctx(args)
    print(json.dumps(sync.sync_dependents(ctx), indent=1))


def cmd_revalidate(args):
    from .stages import extraction
    ctx = _ctx(args)
    print(extraction.revalidate_terms(ctx))


def cmd_runs(args):
    """Every run file under configs/<benchmark|review>/ with what it would run and what runs/ holds for it."""
    from .config import is_benchmark, role_cfg
    root = Path(args.configs)
    files = sorted(p for kind in ("benchmark", "review") for p in (root / kind).rglob("*.yaml")
                   if p.name != "topic.yaml" and "_template" not in p.parts)
    rows = []
    for p in files:
        cfg = load_config(p)
        rd = Path(cfg["run_dir"])
        if not (rd / "store.json").exists():
            status = "not started"
        elif (rd / "report.md").exists():
            status = "review done"
        else:
            status = "review in progress"
        n_eval = len(list((rd / "eval" / "runs").glob("*/results.json")))
        if n_eval:
            status += f", evaluated {n_eval}x"
        a = role_cfg(cfg, "agent")
        rows.append((str(p.relative_to(root)), "benchmark" if is_benchmark(cfg) else "review",
                     f"{a['provider']}:{a['model']}", cfg["pipeline"]["human_mode"],
                     str(cfg["pipeline"]["max_records"] or "all"), status, str(rd)))
    head = ("run file", "kind", "agent", "people", "records", "status", "outputs")
    widths = [max(len(r[i]) for r in rows + [head]) for i in range(len(head))]
    for r in [head] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def cmd_status(args):
    from .stages.report import prisma
    ctx = _ctx(args)
    st = ctx.store
    print("PRISMA:", json.dumps(prisma(ctx)) if st.get("protocol") else "no review yet")
    types = {}
    for n in st.nodes.values():
        types[n["type"]] = types.get(n["type"], 0) + 1
    print("nodes:", types)
    flags = [(n["id"], f) for n in st.nodes.values() for f in n["flags"] if f["status"] == "open"]
    print(f"open flags: {len(flags)}")
    for nid, f in flags[:40]:
        print(f"  [{nid}] {f['id']}: {f['text'][:150]}")
    pend = sorted((ctx.run_dir / "feedback").glob("*.json")) if (ctx.run_dir / "feedback").exists() else []
    for p in pend:
        cs = json.loads(p.read_text())
        if cs["status"] == "proposed":
            print(f"pending change set {cs['id']}: {len(cs['changes'])} changes -> sragent accept --id {cs['id']}")
    cj = ctx.run_dir / "cost.json"
    if cj.exists():
        print("cost:", json.loads(cj.read_text())["spent_usd"], "USD (this run dir, last session)")


def cmd_feedback(args):
    from . import feedback, provenance
    from .stages import report
    ctx = _ctx(args)
    provenance.require_judgment_allowed(ctx, "feedback")
    provenance.record(ctx, "human_judgment", "feedback", target=args.target, note=args.text[:300])
    cs = feedback.handle_feedback(ctx, args.text, target=args.target, pmid=args.pmid,
                                  verify=not args.no_verify, propagate=not args.local_edit,
                                  auto_accept=args.accept)
    print(f"change set {cs['id']}: verdict={cs['verdict']} status={cs['status']} changes={len(cs['changes'])}")
    print("explanation:", cs.get("explanation", ""))
    for c in cs["changes"]:
        print(f"  - {c['role']}: {c['node']}")
    if cs["status"] == "accepted":
        report.write_reports(ctx)
    elif cs["status"] == "proposed":
        print(f"Review the proposal in {ctx.run_dir}/feedback/{cs['id']}.json, then: sragent accept --config {args.config} --id {cs['id']}")


def cmd_rerun(args):
    from . import actions
    ctx = _ctx(args)
    cs = actions.rerun(ctx, args.rid, args.step, note=args.note or "", propagate=not args.no_propagate,
                       fresh=not args.cached)
    print(f"change set {cs['id']}: status={cs['status']} changes={len(cs['changes'])} cost=${cs['cost_usd']}")
    for c in cs["changes"]:
        print(f"  - {c['role']}: {c['node']}")
    if cs["status"] == "proposed":
        print(f"Review it in the UI To-do list, or: sragent accept --config {args.config} --id {cs['id']}")


def cmd_propagate(args):
    from . import actions
    ctx = _ctx(args)
    cs = actions.propagate_from(ctx, args.node, args.from_version, note=args.note or "")
    print(f"change set {cs['id']}: status={cs['status']} changes={len(cs['changes'])} cost=${cs['cost_usd']}")
    for c in cs["changes"]:
        print(f"  - {c['role']}: {c['node']}")


def cmd_accept(args):
    from . import feedback, provenance
    from .stages import report
    ctx = _ctx(args)
    provenance.require_judgment_allowed(ctx, "accept a proposal")
    provenance.record(ctx, "human_judgment", "cs:accept", target=args.id)
    cs = feedback.accept(ctx, args.id)
    report.write_reports(ctx)
    print(f"accepted {cs['id']} ({len(cs['changes'])} changes); report regenerated")


def cmd_reject(args):
    from . import feedback, provenance
    ctx = _ctx(args)
    provenance.require_judgment_allowed(ctx, "reject a proposal")
    provenance.record(ctx, "human_judgment", "cs:reject", target=args.id)
    feedback.reject(ctx, args.id, args.note or "")
    print("rejected", args.id)


def cmd_eval(args):
    from .eval import run_all
    ctx = _ctx(args)
    R = run_all.run(ctx, args.suite, n_cases=args.n_cases, allow_cache=args.allow_cache, from_run=args.from_run,
                    label=args.label or "", resume=args.resume)
    print(f"evaluation run {R['manifest']['run_id']}: cost ${R['cost']['this_run_usd']}")
    print(f"report: {ctx.run_dir / 'eval' / 'runs' / R['manifest']['run_id'] / 'EVAL_REPORT.md'}")


def cmd_eval_audit(args):
    from .eval import run_all
    ctx = _ctx(args)
    print(json.dumps(run_all.import_audit(ctx, args.run, args.file), indent=1))


def cmd_data(args):
    from .sources import http, synergy
    http.init_cache(".cache")
    synergy.load(args.synergy, f"data/{args.synergy.lower()}_records.jsonl")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sragent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", required=True)
        p.add_argument("--budget", type=float, default=None, help="override budget_usd")
        p.add_argument("--run-dir", default=None, help="override run_dir")

    p = sub.add_parser("run"); common(p)
    p.add_argument("--until", default="report",
                   choices=["plan", "search", "screen", "fulltext", "snowball", "extract", "rob", "synthesize", "report"])
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--fresh", action="store_true", help="ignore an existing store.json in run_dir")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status"); common(p); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("runs", help="list every run file under configs/ with its status")
    p.add_argument("--configs", default="configs"); p.set_defaults(fn=cmd_runs)
    p = sub.add_parser("ui", help="open the local web interface")
    common(p)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("pdfs", help="process full-text PDFs added to <run_dir>/fulltext (or --list the wanted ones)")
    common(p)
    p.add_argument("--list", action="store_true")
    p.set_defaults(fn=cmd_pdfs)
    p = sub.add_parser("sync", help="update extraction/RoB/synthesis after human decisions")
    common(p); p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("revalidate-terms", help="re-run database term validation on stored extractions (no LLM)")
    common(p); p.set_defaults(fn=cmd_revalidate)
    p = sub.add_parser("rescreen", help="re-derive eligibility decisions after editing protocol.yaml (no LLM calls)")
    common(p); p.set_defaults(fn=cmd_rescreen)

    p = sub.add_parser("feedback"); common(p)
    p.add_argument("--text", required=True)
    p.add_argument("--target", default=None, help="node id, e.g. ext:30157389, screen:30157389, syn:S2")
    p.add_argument("--pmid", default=None, help="add a paper by PMID")
    p.add_argument("--local-edit", action="store_true", help="baseline: revise only the target")
    p.add_argument("--no-verify", action="store_true", help="apply feedback without checking evidence")
    p.add_argument("--accept", action="store_true", help="apply immediately (skip human confirmation)")
    p.set_defaults(fn=cmd_feedback)

    p = sub.add_parser("rerun", help="re-run one agent step for one study (proposed as a change set)")
    common(p)
    p.add_argument("--rid", required=True)
    p.add_argument("--step", required=True, choices=["screen", "extract", "rob"])
    p.add_argument("--note", default="", help="reviewer note added to the prompt")
    p.add_argument("--no-propagate", action="store_true", help="do not revise dependent outputs")
    p.add_argument("--cached", action="store_true", help="allow a cached answer (default: ask the model again)")
    p.set_defaults(fn=cmd_rerun)
    p = sub.add_parser("propagate", help="revise outputs that depend on a manually edited node (proposal)")
    common(p)
    p.add_argument("--node", required=True)
    p.add_argument("--from-version", type=int, default=None)
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_propagate)

    p = sub.add_parser("accept"); common(p); p.add_argument("--id", required=True); p.set_defaults(fn=cmd_accept)
    p = sub.add_parser("reject"); common(p); p.add_argument("--id", required=True); p.add_argument("--note")
    p.set_defaults(fn=cmd_reject)

    p = sub.add_parser("eval", help="one self-contained evaluation run (eval/runs/<run_id>/)"); common(p)
    p.add_argument("--suite", default="all",
                   choices=["all", "e2e", "screening", "stagewise", "grounding", "synthesis", "claim1", "claim2",
                            "claim2_rescore", "judge"])
    p.add_argument("--n-cases", type=int, default=None, help="cases per Claim-2 category (cost control)")
    p.add_argument("--allow-cache", action="store_true", help="reuse identical earlier model calls (default: fresh calls)")
    p.add_argument("--from-run", default=None, help="reuse that run's stage-wise review, or the run to re-score (claim2_rescore)")
    p.add_argument("--label", default=None, help="short label added to the run id")
    p.add_argument("--resume", default=None, help="continue an interrupted evaluation run (same directory and snapshot)")
    p.set_defaults(fn=cmd_eval)
    p = sub.add_parser("eval-audit", help="import a filled human audit sheet into an evaluation run"); common(p)
    p.add_argument("--run", required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_eval_audit)

    p = sub.add_parser("data"); p.add_argument("--synergy", required=True); p.set_defaults(fn=cmd_data)

    p = sub.add_parser("local-endpoints", help="inspect or atomically update live local model endpoints")
    p.add_argument("--config", required=True)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--nodes", nargs="*", default=None, help="replace the list with SSH node names; empty clears it")
    group.add_argument("--urls", nargs="*", default=None, help="replace the list with existing endpoint URLs")
    p.add_argument("--model", default="auto", help="served model ID, or auto for single-model servers")
    p.add_argument("--remote-host", default="127.0.0.4")
    p.add_argument("--remote-port", type=int, default=8000)
    p.add_argument("--slots", type=int, default=1, help="maximum active requests per endpoint in this process")
    p.add_argument("--roles", nargs="*", default=None, choices=["agent", "judge", "reviewer", "judge2"],
                   help="restrict the new endpoints to these roles (default: every role)")
    p.add_argument("--add", action="store_true", help="append to the current list instead of replacing it")
    p.add_argument("--probe", action="store_true", help="connect and list served models (no inference)")
    from .local import cli_endpoints
    p.set_defaults(fn=cli_endpoints)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

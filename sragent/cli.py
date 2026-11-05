"""Command-line interface.

  sragent run       --config C [--until STAGE] [--plan-only] [--fresh]
  sragent ui        [--config C] [--port 8765] (local web interface; lists, edits and opens every run file)
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
  sragent accept-config --config C --reason "..."   (continue a started review whose fixed settings were edited)
  sragent local-endpoints --config C [--nodes N ... | --urls U ...] [--roles R ...] [--add] [--probe]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .context import Ctx


def _ctx(args, fresh=False, check_config=True, write=True):
    """The review of a command. A command that may change it holds the run folder's lease for its whole life
    (sragent/lease.py): one writer at a time, also against the web interface and its jobs."""
    over = {}
    if getattr(args, "budget", None) is not None:
        over["budget_usd"] = args.budget
    if getattr(args, "run_dir", None):
        over["run_dir"] = args.run_dir
    cfg = load_config(args.config, over)
    if write:
        from . import lease
        lease.acquire(cfg["run_dir"])
    return Ctx.create(cfg, fresh=fresh, check_config=check_config)


def cmd_accept_config(args):
    """Accept edited review-defining settings on purpose; the change is recorded (config_lock.py)."""
    from . import config_lock
    ctx = _ctx(args, check_config=False)
    rec = (ctx.store.meta.get("config_lock") or {}).get("values")
    ch = config_lock.diff(rec, ctx.cfg) if rec else []
    if not ch:
        print("Nothing to accept: the config matches the settings this review started with.")
        return
    for c in ch:
        print(f"  {c['key']}:\n    was: {config_lock.short(c['old'])}\n    now: {config_lock.short(c['new'])}")
    if not args.yes:
        try:
            ok = input("Accept these changes for this review? They are recorded and shown in the report [y/N] ").strip().lower() == "y"
        except EOFError:   # no terminal: only --yes accepts
            ok = False
        if not ok:
            print("\nNot accepted (use --yes when there is no terminal to answer).")
            return 1
    config_lock.accept(ctx, args.reason)
    print(f"Accepted and recorded in {ctx.run_dir / 'provenance.jsonl'}.")


def cmd_run(args):
    from . import pipeline
    ctx = _ctx(args, fresh=args.fresh)
    pipeline.run(ctx, until=args.until, stop_after_plan=args.plan_only)
    print(json.dumps(ctx.hub.tracker.summary(), indent=1))


def cmd_rescreen(args):
    """Apply a hand-edited protocol.yaml (operation protocol.import_file)."""
    _op(args, "protocol.import_file", {})


def cmd_ui(args):
    from .ui.server import serve
    serve(args.config, host=args.host, port=args.port, open_browser=not args.no_browser, configs_dir=args.configs)


def cmd_pdfs(args):
    from .stages.fulltext import fulltext_dir, write_needed_pdfs
    ctx = _ctx(args)
    if args.list:
        p = write_needed_pdfs(ctx)
        print(f"Put PDFs in {fulltext_dir(ctx)} named <rid>.pdf. Wanted files: {p}")
        print(p.read_text())
        return
    _op(args, "retrieve.process_added_files", {}, ctx)


def cmd_sync(args):
    _op(args, "update_all", {})


def cmd_revalidate(args):
    _op(args, "extract.revalidate_terms", {})


def cmd_runs(args):
    """Every run file under configs/<benchmark|review>/ with what it would run and what runs/ holds for it."""
    from .config import is_benchmark, role_cfg
    from .config_lock import pending
    from .ui.configs import run_status
    root = Path(args.configs)
    files = sorted(p for kind in ("benchmark", "review") for p in (root / kind).rglob("*.yaml")
                   if p.name != "topic.yaml" and "_template" not in p.parts)
    rows = []
    for p in files:
        try:
            cfg = load_config(p)
        except Exception as e:   # one broken run file should not hide the others
            rows.append((str(p.relative_to(root)), "?", "", "", "", f"does not load: {e}"[:90], ""))
            continue
        rd = Path(cfg["run_dir"])
        status = run_status(rd)
        if pending(cfg, rd):
            status += ", PAUSED: fixed settings changed (sragent accept-config)"
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
    ctx = _ctx(args, write=False)
    st = ctx.store
    print("PRISMA:", json.dumps(prisma(ctx)) if st.get("protocol") else "no review yet")
    types = {}
    for n in st.nodes.values():
        types[n["type"]] = types.get(n["type"], 0) + 1
    print("nodes:", types)
    from . import graph
    ss = graph.stale_summary(st)
    print(f"out of date (a dependency changed since they were computed): {ss['n']} {ss['by_type']}"
          + (f"; {ss['pinned']} of them decided or edited by a person (never overwritten)" if ss["pinned"] else ""))
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
    r = _op(args, "feedback", {"text": args.text, "target": args.target, "pmid": args.pmid, "verify": not args.no_verify,
                               "propagate": not args.local_edit, "auto_accept": args.accept})
    if r.get("status") == "proposed":
        print(f"Review the proposal, then: sragent op proposal.accept --config {args.config} --args '{{\"changeset\": \"{r['changeset']}\"}}'")


def cmd_rerun(args):
    name = {"screen": "screen.recompute", "extract": "extract.recompute", "rob": "rob.recompute"}[args.step]
    _op(args, name, {"rid": args.rid, "note": args.note or "", "propagate": not args.no_propagate, "fresh": not args.cached})


def cmd_propagate(args):
    _op(args, "edit.update_dependents", {"node": args.node, "from_version": args.from_version, "note": args.note or ""})


def cmd_accept(args):
    _op(args, "proposal.accept", {"changeset": args.id})


def cmd_reject(args):
    _op(args, "proposal.reject", {"changeset": args.id, "note": args.note or ""})


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


def cmd_verify_screening(args):
    if args.mode == "all":
        _op(args, "screen.verify", {"rids": args.rids or None})
    else:
        _op(args, "eligibility.verify", {"rids": args.rids or None, "mode": args.mode})


def cmd_verify_rob(args):
    _op(args, "rob.verify", {"rids": args.rids or None})


def cmd_reassess_rob(args):
    _op(args, "rob.redraft_all", {"rids": args.rids or None})


def cmd_fetch_files(args):
    _op(args, "retrieve.fetch", {})


def cmd_eval_summary(args):
    from .eval import run_all
    ctx = _ctx(args)
    run_id = args.run or (ctx.run_dir / "eval" / "LATEST_RUN.txt").read_text().strip()
    row = run_all.summary_row(ctx, run_id, note=args.note or "", run_file=args.config)
    if args.append:
        p = Path(args.append)
        text = p.read_text() if p.exists() else ""
        has_header = "| " + run_all.SUMMARY_COLUMNS[0] + " |" in text
        with open(p, "a") as f:
            f.write(("" if text.endswith("\n") or not text else "\n") + run_all.summary_markdown([row], header=not has_header) + "\n")
        print(f"appended to {p}")
    print(run_all.summary_markdown([row]))


def cmd_data(args):
    from .sources import http, synergy
    http.init_cache(".cache")
    synergy.load(args.synergy, f"data/{args.synergy.lower()}_records.jsonl")


def _op(args, name: str, a: dict, ctx=None) -> dict:
    """Run one operation for a command and print its result (the commands below are names for operations)."""
    from . import ops
    res = ops.run(ctx or _ctx(args), name, a)
    print(json.dumps(res, indent=1, default=str))
    if not res.get("ok"):
        raise SystemExit(1)
    return res


def cmd_op(args):
    """sragent op --list | sragent op NAME --config C [--args JSON]: one operation (sragent/ops)."""
    from . import ops
    if args.list or not args.name:
        for o in ops.listing():
            print(f"{o['stage']:12s} {o['name']:34s} {o['kind']:13s} {', '.join(o['params'])}")
            print(f"{'':12s} {o['summary']}")
        return
    if not args.config:
        raise SystemExit("--config is required to run an operation")
    ctx = _ctx(args)
    res = ops.run(ctx, args.name, json.loads(args.args or "{}"))
    print(json.dumps(res, indent=1, default=str))
    if not res.get("ok"):
        raise SystemExit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sragent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", required=True)
        p.add_argument("--budget", type=float, default=None, help="override budget_usd")
        p.add_argument("--run-dir", default=None, help="override run_dir")

    p = sub.add_parser("run"); common(p)
    from .stagespec import KEYS, LEGACY
    p.add_argument("--until", default="report", choices=list(KEYS) + list(LEGACY),
                   help=f"stop after this stage: {', '.join(KEYS)} (earlier names {', '.join(LEGACY)} are accepted)")
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--fresh", action="store_true", help="ignore an existing store.json in run_dir")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status"); common(p); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("runs", help="list every run file under configs/ with its status")
    p.add_argument("--configs", default="configs"); p.set_defaults(fn=cmd_runs)
    p = sub.add_parser("ui", help="open the local web interface (every run file under configs/ can be opened in it)")
    p.add_argument("--config", default=None, help="run file to open first (default: choose one on the Configs page)")
    p.add_argument("--configs", default=None, help="configs folder (default: the one --config is in, else ./configs)")
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

    p = sub.add_parser("verify-screening", help="a second reading (roles.verifier) of screening decisions already made; disagreements go to a person"); common(p)
    p.add_argument("--mode", default="pending", choices=["pending", "full_text", "all"],
                   help="pending: records undecided after their full text; full_text: every full-text decision too; all: also title/abstract exclusions")
    p.add_argument("--rids", nargs="*", default=None, help="only these record ids")
    p.set_defaults(fn=cmd_verify_screening)

    p = sub.add_parser("verify-rob", help="the verifier's second look (roles.verifier) at risk-of-bias drafts flagged with a reason; what it cannot settle goes to a person"); common(p)
    p.add_argument("--rids", nargs="*", default=None, help="only these record ids")
    p.set_defaults(fn=cmd_verify_rob)

    p = sub.add_parser("reassess-rob", help="draft every included study's risk of bias again with the current prompt and settings (confirmed drafts are kept)"); common(p)
    p.add_argument("--rids", nargs="*", default=None, help="only these record ids")
    p.set_defaults(fn=cmd_reassess_rob)

    p = sub.add_parser("fetch-files", help="keep the agent-fetched full texts of an existing review as files (fulltext/auto/: text, and the PMC PDF when available)"); common(p)
    p.set_defaults(fn=cmd_fetch_files)

    p = sub.add_parser("eval-summary", help="one comparable Markdown row for an evaluation run (results.json + manifest)"); common(p)
    p.add_argument("--run", default=None, help="evaluation run id (default: the run's latest)")
    p.add_argument("--note", default="", help="what changed / what to keep in mind for this run")
    p.add_argument("--append", default=None, help="append the row to this Markdown file (e.g. docs/RESULTS.md)")
    p.set_defaults(fn=cmd_eval_summary)

    p = sub.add_parser("data"); p.add_argument("--synergy", required=True); p.set_defaults(fn=cmd_data)
    p = sub.add_parser("op", help="run one operation (sragent op --list shows them)")
    p.add_argument("name", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--config")
    p.add_argument("--args", default="{}", help="the operation's arguments as JSON")
    p.add_argument("--budget", type=float, default=None)
    p.add_argument("--run-dir", default=None)
    p.set_defaults(fn=cmd_op)

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

    p = sub.add_parser("accept-config", help="continue a started review whose review-defining settings were edited")
    common(p)
    p.add_argument("--reason", required=True, help="why the settings changed (recorded and shown in the report)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(fn=cmd_accept_config)

    args = ap.parse_args(argv)
    from .config_lock import ConfigChanged
    try:
        return args.fn(args)
    except ConfigChanged as e:
        print("sragent: " + str(e), file=sys.stderr)
        for c in e.changes:
            print(f"  {c['key']}:\n    started with: {_short(c['old'])}\n    config now:   {_short(c['new'])}", file=sys.stderr)
        return 2


def _short(v):
    from .config_lock import short
    return short(v)


if __name__ == "__main__":
    sys.exit(main())

"""Command-line systematic review tools."""
from __future__ import annotations

import argparse
import json
import sys

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
    from .stages import planning, screening
    import yaml
    ctx = _ctx(args)
    protocol = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    ctx.store.upsert("protocol", "protocol", protocol, actor="user", reason="protocol edited by user")
    for c in protocol.get("criteria", []):
        ctx.store.upsert(f"crit:{c['id']}", "criterion", c, depends_on=["protocol"], actor="user", reason="protocol edited")
    print(screening.rescreen_rule_only(ctx, protocol))


def cmd_status(args):
    ctx = _ctx(args)
    st = ctx.store
    types = {}
    for n in st.nodes.values():
        types[n["type"]] = types.get(n["type"], 0) + 1
    print("nodes:", types)
    flags = [(n["id"], f) for n in st.nodes.values() for f in n["flags"] if f["status"] == "open"]
    print(f"open flags: {len(flags)}")
    for nid, f in flags[:40]:
        print(f"  [{nid}] {f['id']}: {f['text'][:150]}")
    cj = ctx.run_dir / "cost.json"
    if cj.exists():
        print("cost:", json.loads(cj.read_text())["spent_usd"], "USD (this run dir, last session)")


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
    p.add_argument("--until", default="screen",
                   choices=['plan', 'search', 'screen'])
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--fresh", action="store_true", help="ignore an existing store.json in run_dir")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status"); common(p); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("rescreen", help="re-derive eligibility decisions after editing protocol.yaml (no LLM calls)")
    common(p); p.set_defaults(fn=cmd_rescreen)

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
    p.add_argument("--probe", action="store_true", help="connect and list served models (no inference)")
    from .local import cli_endpoints
    p.set_defaults(fn=cli_endpoints)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

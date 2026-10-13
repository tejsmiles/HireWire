"""onboard-sync command line.

  onboard-sync scenarios              list the demo scenarios
  onboard-sync replay S08             run one scenario (persisted to ./var so you can inspect it)
  onboard-sync replay all             run every scenario, each on a fresh in-memory world
  onboard-sync hires | show <hire> | diff <hire> | exceptions | audit [--verify]
  onboard-sync resolve <exception_id> <option>
  onboard-sync sweep | serve | mcp | reset
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from .config import Settings
from .scenarios import SCENARIOS, T0, run
from .service import Service
from .timeutil import Clock


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _home() -> Path:
    return Path(os.getenv("ONBOARD_SYNC_HOME", "var"))


def _settings(live: bool) -> Settings:
    if live and not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("--live-claude needs ANTHROPIC_API_KEY")
    return Settings(claude_mode="live" if live else "stub", retry_base_seconds=0)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="onboard-sync", description="Onboarding change-propagation engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scenarios", help="list demo scenarios")
    r = sub.add_parser("replay", help="run a demo scenario, or 'all'")
    r.add_argument("scenario")
    r.add_argument("--live-claude", action="store_true", help="use the Anthropic API instead of the stub")
    sub.add_parser("hires", help="list hires")
    s = sub.add_parser("show", help="full view of one hire (id or name)")
    s.add_argument("hire")
    d = sub.add_parser("diff", help="dry-run plan for one hire")
    d.add_argument("hire")
    e = sub.add_parser("exceptions", help="open exceptions")
    e.add_argument("--all", action="store_true")
    rs = sub.add_parser("resolve", help="resolve an exception")
    rs.add_argument("exception_id")
    rs.add_argument("option")
    rs.add_argument("--actor", default=f"cli:{os.getenv('USER', 'operator')}")
    sub.add_parser("sweep", help="run the anti-entropy sweep")
    a = sub.add_parser("audit", help="audit trail (optionally for one hire)")
    a.add_argument("hire", nargs="?")
    a.add_argument("--verify", action="store_true")
    sv = sub.add_parser("serve", help="run the HTTP API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sub.add_parser("mcp", help="run the MCP server on stdio")
    sub.add_parser("reset", help="delete ./var state")
    args = p.parse_args(argv)

    if args.cmd == "scenarios":
        for sid, (title, _) in SCENARIOS.items():
            print(f"{sid}  {title}")
        return
    if args.cmd == "reset":
        shutil.rmtree(_home(), ignore_errors=True)
        print(f"removed {_home()}/")
        return
    if args.cmd == "replay":
        settings = _settings(args.live_claude)
        if args.scenario.lower() == "all":
            for sid in SCENARIOS:
                run(sid, svc=Service(clock=Clock(T0), settings=settings))
            print(f"\nReplayed {len(SCENARIOS)} scenarios. Assertions live in tests/: run `pytest`.")
            return
        sid = args.scenario.upper()
        if sid not in SCENARIOS:
            raise SystemExit(f"unknown scenario {sid}; see `onboard-sync scenarios`")
        shutil.rmtree(_home(), ignore_errors=True)
        _home().mkdir(parents=True)
        svc = Service(str(_home() / "onboard.db"), _home() / "world.json", Clock(T0), settings)
        run(sid, svc=svc)
        svc.world.save()
        print(f"\nState persisted to {_home()}/ (try: onboard-sync hires | exceptions --all | audit --verify)")
        return
    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return

    svc = Service.from_env()

    def hid(q: str) -> str:
        found = svc.find_hire(q)
        if not found:
            raise SystemExit(f"no hire matches '{q}'")
        return found

    if args.cmd == "hires":
        for h in svc.hires():
            print(f"{h['hire_id']}  {h['name']:<18} start {h['start_date']}  {h['lifecycle']:<9} "
                  f"{h['sync_state']:<9} exceptions={h['open_exceptions']}  systems={','.join(h['systems'])}")
    elif args.cmd == "show":
        _dump(svc.hire(hid(args.hire)))
    elif args.cmd == "diff":
        _dump(svc.diff(hid(args.hire)))
    elif args.cmd == "exceptions":
        _dump(svc.exceptions(include_closed=args.all))
    elif args.cmd == "resolve":
        _dump(svc.resolve(args.exception_id, args.option, actor=args.actor))
    elif args.cmd == "sweep":
        report = svc.sweep()
        _dump({"started": report["started"],
               "reconciled": {h: r.get("sync_state") for h, r in report["reconciled"].items()}})
    elif args.cmd == "audit":
        if args.verify:
            _dump(svc.verify_audit())
        else:
            for a in svc.audit(hid(args.hire) if args.hire else None):
                print(f"#{a['seq']:<4} {a['ts'][:16]}  {a['actor']:<22} {a['action']:<20} {a['hash']}")
    svc.world.save()


if __name__ == "__main__":
    main()

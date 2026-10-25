"""Command line companion to the app (same database and data folder).

  python -m backend.cli traders                         list tracked traders
  python -m backend.cli add 0xWALLET|@name --days 7     add a trader and download
  python -m backend.cli update <trader> [--days N]      download missing data + analyse
  python -m backend.cli analyze <trader>                re-run the analysis on stored data
  python -m backend.cli live <trader>                   print a trader's trades as they happen
  python -m backend.cli export <trader> [--prices]      write a zip next to the project
  python -m backend.cli bots                            list bots
  python -m backend.cli bot start|stop <bot_id>         control a bot process
  python -m backend.cli bot archive|unarchive <bot_id>  stop + hide a bot from the Bots page (data kept) / bring it back
"""
import argparse
import time
from datetime import datetime, timedelta, timezone

from .bots import control, registry
from .config import ROOT
from .traders import export, pipeline, store
from .traders.sources import parse_identity, resolve_handle
from .util import read_json


def _find(key):
    for t in store.list_traders():
        if key in (t.id, t.name) or key.lower() == t.wallet:
            return t
    raise SystemExit(f"no trader '{key}'")


def _period(days):
    end = datetime.now(timezone.utc).date()
    return end - timedelta(days=max(days - 1, 1)), end


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("traders")
    a = sub.add_parser("add")
    a.add_argument("ident")
    a.add_argument("--name", default="")
    a.add_argument("--asset", default="BTC")
    a.add_argument("--window", type=int, default=5)
    a.add_argument("--days", type=int, default=7)
    u = sub.add_parser("update")
    u.add_argument("trader")
    u.add_argument("--days", type=int, default=None)
    sub.add_parser("analyze").add_argument("trader")
    sub.add_parser("live").add_argument("trader")
    e = sub.add_parser("export")
    e.add_argument("trader")
    e.add_argument("--prices", action="store_true")
    sub.add_parser("bots")
    b = sub.add_parser("bot")
    b.add_argument("action", choices=["start", "stop", "archive", "unarchive"])
    b.add_argument("bot_id")
    args = ap.parse_args()

    if args.cmd == "traders":
        for t in store.list_traders():
            S = read_json(t.analysis_dir / "summary.json", {}) or {}
            print(f"{t.id:24s} {t.wallet}  {t.asset} {t.window_minutes}m  pnl {S.get('total_pnl')}")
    elif args.cmd == "add":
        kind, val = parse_identity(args.ident)
        handle = ""
        if kind == "handle":
            handle, (val, _) = val, resolve_handle(val)
        t = store.create(args.name or handle or store.short_wallet(val), val, args.asset, args.window, handle)
        pipeline.run_update(t.id, *_period(args.days))
    elif args.cmd == "update":
        t = _find(args.trader)
        start, end = _period(args.days) if args.days else (None, datetime.now(timezone.utc).date())
        if start is None:
            start = datetime.fromtimestamp(t.coverage[0][0], timezone.utc).date() if t.coverage else _period(7)[0]
        pipeline.run_update(t.id, start, end)
    elif args.cmd == "analyze":
        pipeline.run_update(_find(args.trader).id, fetch=False)
    elif args.cmd == "live":
        from .traders.live import LiveHub
        t = _find(args.trader)
        hub = LiveHub()
        hub.watch(t.wallet, t.id, t.asset, t.window_minutes,
                  on_event=lambda ev: print(f"{ev['source']:7s} {ev['kind']:6s} {ev.get('side', ''):4s} "
                                            f"{ev.get('outcome')} {ev.get('size')} @ {ev.get('price')} {ev.get('slug')}"))
        while True:
            time.sleep(1)
    elif args.cmd == "export":
        t = _find(args.trader)
        out = ROOT / f"{t.id}_{time.strftime('%Y%m%d-%H%M')}.zip"
        out.write_bytes(export.build_zip([t], args.prices))
        print(out)
    elif args.cmd == "bots":
        for bt in registry.list_bots():
            print(f"{bt['id']:24s} {bt['strategy']:14s} {bt['mode']:6s} running={control.is_running(bt['id'])}"
                  + (" archived" if bt.get("archived") else ""))
    elif args.cmd == "bot":
        getattr(control, args.action)(args.bot_id)


if __name__ == "__main__":
    main()

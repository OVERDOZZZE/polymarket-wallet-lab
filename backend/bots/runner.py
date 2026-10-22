"""Process entry point for one bot:  python -m backend.bots.runner <bot_id> [--quiet]

Started by backend/bots/control.py (the UI's Start button). Raises its own priority, writes
data/bots/<id>/state.json for the UI and obeys data/bots/<id>/control.json.
"""
import json
import os
import sys
import time

from . import registry
from .control import bot_dir
from ..util import high_priority, pid_alive


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: python -m backend.bots.runner <bot_id> [--quiet]")
    bot_id, quiet = args[0], "--quiet" in sys.argv
    high_priority()
    row = registry.get_bot(bot_id)
    engine_cls, _ = registry.strategy(row["strategy"])
    bot = engine_cls(bot_id)
    bot.publish_state = True
    d = bot_dir(bot_id)
    pid_path = d / "bot.pid"
    try:
        other = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        other = None
    if other and other not in (os.getpid(), os.getppid()) and pid_alive(other):   # systemd + UI; ppid = venv launcher
        sys.exit(f"bot {bot_id} is already running (pid {other})")
    pid_path.write_text(str(os.getpid()))
    bot.start()
    if not quiet:
        print(f"bot {bot_id} running (pid {os.getpid()}). Ctrl+C to stop.")
    try:
        last = 0.0
        while bot.running or (bot._thread and bot._thread.is_alive()):
            time.sleep(0.5)
            if quiet or time.time() - last < 5:
                continue
            last = time.time()
            s = bot.snapshot()
            if s:
                t = s["totals"]
                print(f"{time.strftime('%H:%M:%S')}  fills {t['fills']}  vol ${t['volume']:,.0f}  "
                      f"trading ${s['trading_pnl']:+,.2f}  rebates ${t['rebate_est']:,.2f}", flush=True)
    except KeyboardInterrupt:
        bot.stop()
        if bot._thread:
            bot._thread.join(timeout=5)
    finally:
        snap = dict(bot.snapshot() or {})
        snap.update(running=False, ts=time.time())
        try:
            (d / "state.json").write_text(json.dumps(snap, default=str), encoding="utf-8")
        except Exception:
            pass
        pid_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

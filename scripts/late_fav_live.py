"""The live late-favourite v3 test on the server (run from ~/app):

  .venv/bin/python -m scripts.late_fav_live create   # bot late-fav-v3-live: v3's settings, live_size 25 sh, $50 budget,
                                                     # $20 loss limit; paper mode + dry run (sends nothing)
  .venv/bin/python -m scripts.late_fav_live dry      # paper mode + dry run: live order flow with simulated acks
  .venv/bin/python -m scripts.late_fav_live arm      # mode = live, dry run off: REAL orders from its next start
  .venv/bin/python -m scripts.late_fav_live disarm   # mode = paper, dry run off (from its next start)

The mode is read when the bot starts: restart it (sudo systemctl restart pmtool-bot@late-fav-v3-live) afterwards.
The kill switch (live_paused) works at once without a restart.
"""
import os
import sys

from backend.bots import control, registry

BOT, SRC = "late-fav-v3-live", "late-fav-v3"


def _set(mode, dry, note):
    control.save_config(BOT, {"live_dry_run": dry})
    registry.update_bot(BOT, mode=mode)
    registry.add_change(BOT, note, "settings")
    print(BOT, "mode", mode, "dry run", dry, "- restart the bot for it to take effect")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create":
        cfg = {**registry.get_bot(SRC)["config"], "live_size": 25.0, "live_budget_usd": 50.0, "live_loss_limit_usd": 20.0,
               "live_paused": False, "live_dry_run": True}
        b = registry.create_bot("Late favourite v3 LIVE", "late_fav", cfg, mode="paper", bot_id=BOT)
        os.makedirs(f"data/bots/{b['id']}", exist_ok=True)   # systemd opens bot.log here before its own mkdir runs
        registry.add_change(b["id"], f"Copy of {SRC} (lock gate from 120 s, z 3, Chainlink vol, 0.99) for a real-money "
                                     "test the user asked for: live bids of 25 shares per window, at most $50 committed, "
                                     "no new bids once settled live PnL <= -$20 (one lost window). Starts in dry run.",
                           "settings")
        print("created", b["id"], "mode", b["mode"], "dry run")
    elif cmd == "dry":
        _set("paper", True, "Dry run: live order flow with simulated acks, nothing sent.")
    elif cmd == "arm":
        _set("live", False, "Mode set to live: real post-only bids from the next start (user's request).")
    elif cmd == "disarm":
        _set("paper", False, "Mode set to paper (no live orders from the next start).")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

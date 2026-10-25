"""Set up the live imbalance maker on the server (run from ~/app):

  .venv/bin/python -m scripts.imb_maker_live create   # bot imb-maker-live: imb-maker-300's settings (B 5, cap 25), $100 per
                                                      # window, $60 loss limit; paper mode + dry run (sends nothing)
  .venv/bin/python -m scripts.imb_maker_live dry      # paper mode + dry run: live order flow with simulated acks
  .venv/bin/python -m scripts.imb_maker_live arm      # mode = live, dry run off: REAL orders from its next start
  .venv/bin/python -m scripts.imb_maker_live disarm   # mode = paper, dry run off (from its next start)

The mode is read when the bot starts: restart it (sudo systemctl restart pmtool-bot@imb-maker-live) afterwards.
The kill switch (live_paused, Settings -> Live) works at once without a restart.
"""
import sys

from backend.bots import control, registry

BOT, SRC = "imb-maker-live", "imb-maker-300"


def _set(mode, dry, note):
    control.save_config(BOT, {"live_dry_run": dry})
    registry.update_bot(BOT, mode=mode)
    registry.add_change(BOT, note, "settings")
    print(BOT, "mode", mode, "dry run", dry, "- restart the bot for it to take effect")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create":
        cfg = {**registry.get_bot(SRC)["config"], "bankroll": 300.0, "spend_cap_usd": 100.0, "live_loss_limit_usd": 60.0,
               "live_paused": False, "live_merge": True, "live_dry_run": True}
        b = registry.create_bot("Imbalance maker LIVE", "imb_maker", cfg, mode="paper", bot_id=BOT)
        registry.add_change(b["id"], f"Copy of {SRC}'s settings (B 5, inv_cap 25) for BOT_SPEC.md phase 2: spend cap $100 per "
                                     "window, live loss limit $60, merge pairs after each window. Starts in dry run "
                                     "(nothing sent).", "settings")
        print("created", b["id"], "mode", b["mode"], "dry run")
    elif cmd == "dry":
        _set("paper", True, "Dry run: live order flow with simulated acks, nothing sent.")
    elif cmd == "arm":
        _set("live", False, "Mode set to live: real post-only orders from the next start.")
    elif cmd == "disarm":
        _set("paper", False, "Mode set to paper (no live orders from the next start).")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

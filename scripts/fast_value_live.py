"""Set up the live Fast player (value) bot on the server (run from ~/app):

  .venv/bin/python -m scripts.fast_value_live create   # bot fast-value-live: fast-value's settings, fresh $500 paper twin
  .venv/bin/python -m scripts.fast_value_live arm      # mode = live (real orders from its next start)
  .venv/bin/python -m scripts.fast_value_live disarm   # mode = paper (from its next start; pause is instant in Settings)

The mode is read when the bot starts: restart it (sudo systemctl restart pmtool-bot@fast-value-live) after arm/disarm.
"""
import sys

from backend.bots import registry

BOT, SRC = "fast-value-live", "fast-value"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create":
        cfg = {**registry.get_bot(SRC)["config"], "bankroll": 500.0, "live_scale": 0.2, "live_loss_limit_usd": 20.0,
               "live_max_order_usd": 25.0, "live_paused": False}
        b = registry.create_bot("Fast player (value) LIVE", "fast_player", cfg, mode="paper", bot_id=BOT)
        registry.add_change(b["id"], f"Copy of {SRC}'s settings. Live: real buys of paper $ x 0.2 next to a $500 "
                                     "paper twin, $20 live loss limit (open $ counted as lost).", "settings")
        print("created", b["id"], "mode", b["mode"])
    elif cmd in ("arm", "disarm"):
        mode = "live" if cmd == "arm" else "paper"
        registry.update_bot(BOT, mode=mode)
        registry.add_change(BOT, f"Mode set to {mode}.", "settings")
        print(BOT, "mode", mode, "- restart the bot for it to take effect")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

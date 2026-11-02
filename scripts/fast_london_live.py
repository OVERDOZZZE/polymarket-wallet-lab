"""The fast player (value) on London: does the taker fill rate change after the move? (LONDON_PLAN.md, run on the
server from ~/app)

  .venv/bin/python -m scripts.fast_london_live create   # bot fast-london: fast-value-live's signal unchanged (cb + kr + bn
                                                        # spike 2 bps, max price 0.60, hold), $1 per live order, $5 live
                                                        # loss limit; paper twin on the queue fill model (queue_max_ms
                                                        # 2200, the model that reproduced Dublin's live fills); mode paper
  .venv/bin/python -m scripts.fast_london_live arm      # mode = live: REAL $1 FAK buys from its next start
  .venv/bin/python -m scripts.fast_london_live disarm   # mode = paper (from its next start; live_paused is instant)
  .venv/bin/python -m scripts.fast_london_live signal binance_fut   # spike signal = Binance futures only (default)
  .venv/bin/python -m scripts.fast_london_live signal classic       # = Coinbase + Kraken + Binance spot (mean)
                                                        # signal: saved, and applied to the running bot at once

Restart after arm / disarm: sudo systemctl restart pmtool-bot@fast-london (first time: bash deploy/install.sh fast-london).
Dublin reference (fast-value-live, 10-23..10-26): 641 live FAKs, 64 filled (10.0%), post p50 655 ms.
"""
import sys

from backend.bots import control, registry
from backend.bots.strategies.fast_player import FEED_PRESETS

BOT, SRC = "fast-london", "fast-value-live"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create":
        cfg = {**registry.get_bot(SRC)["config"], "bankroll": 500.0, "queue_max_ms": 2200.0,
               "live_stake_frac": 0.0, "live_scale": 0.0001, "live_max_order_usd": 1.0,     # every live order = $1
               "live_loss_limit_usd": 5.0, "live_daily_loss_usd": 0.0, "live_paused": False,
               "signal_feeds": "binance_fut"}
        b = registry.create_bot("Fast player (value) London", "fast_player", cfg, mode="paper", bot_id=BOT)
        registry.add_change(b["id"], f"Copy of {SRC}'s signal for the London fill-rate test: $1 per live FAK, $5 live loss "
                                     "limit; paper twin on the queue model (queue_max_ms 2200).", "settings")
        print("created", b["id"], "mode", b["mode"])
    elif cmd in ("arm", "disarm"):
        mode = "live" if cmd == "arm" else "paper"
        registry.update_bot(BOT, mode=mode)
        registry.add_change(BOT, f"Mode set to {mode}.", "settings")
        print(BOT, "mode", mode, "- restart the bot for it to take effect")
    elif cmd == "signal" and len(sys.argv) > 2 and sys.argv[2] in FEED_PRESETS:
        cfg = control.save_config(BOT, {"signal_feeds": sys.argv[2]})
        print(BOT, "signal_feeds", cfg["signal_feeds"], "sources", cfg["sources"], "combine", cfg["combine"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

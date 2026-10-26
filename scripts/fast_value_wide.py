"""Paper copies of fast-value-live with a wider order allowance, filled by the queue model that matches live FAKs
(reports/fast_allowance/README.md). Run on the server from ~/app:

  .venv/bin/python -m scripts.fast_value_wide create     # creates the bots below (paper, fresh $500 each)
  bash deploy/install.sh fast-value-q fast-value-wide fast-value-wider

  fast-value-q       control: fast-value-live's settings, only the realistic fills (compare the others with this one)
  fast-value-wide    + 2c allowance (extra_pay)
  fast-value-wider   + 3c allowance and the price cap at 0.70
"""
import sys

from backend.bots import registry

SRC = "fast-value-live"
QUEUE = {"queue_max_ms": 2200.0, "latency_samples": []}
BOTS = [
    ("fast-value-q", "Fast player (value) realistic fills", {}),
    ("fast-value-wide", "Fast player (value) +2c", {"extra_pay": 0.02}),
    ("fast-value-wider", "Fast player (value) +3c, cap 70c", {"extra_pay": 0.03, "max_price": 0.70}),
]
LIVE_KEYS = ("live_scale", "live_stake_frac", "live_loss_limit_usd", "live_daily_loss_usd", "live_max_order_usd",
             "live_paused")


def main():
    if (sys.argv[1] if len(sys.argv) > 1 else "") != "create":
        sys.exit(__doc__)
    base = {k: v for k, v in registry.get_bot(SRC)["config"].items() if k not in LIVE_KEYS}
    for bot_id, name, ov in BOTS:
        try:
            registry.get_bot(bot_id)
            print(bot_id, "exists, skipped")
            continue
        except KeyError:
            pass
        cfg = {**base, **QUEUE, **ov, "bankroll": 500.0}
        b = registry.create_bot(name, "fast_player", cfg, mode="paper", bot_id=bot_id)
        what = ", ".join(f"{k} {v}" for k, v in ov.items()) or "no strategy change"
        registry.add_change(b["id"], f"Copy of {SRC}'s settings ({what}). Paper fills use the queue model "
                                     "(queue_max_ms 2200), which reproduces live FAK misses (~10% filled at the "
                                     "old settings): see reports/fast_allowance.", "settings")
        print("created", b["id"])


if __name__ == "__main__":
    main()

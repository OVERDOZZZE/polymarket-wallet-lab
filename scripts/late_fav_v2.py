"""Create the late_fav v2 paper bots on the server (run from ~/app), next to late-fav (v1) on the same windows:

  .venv/bin/python -m scripts.late_fav_v2 create

  late-fav-v2         gate "lock": 0.99 bids only, 1000 shares, once the final TWAP is locked (z >= 5 and >= $3)
  late-fav-v2-ladder  the same gate, bids at 0.97 / 0.98 / 0.99 (300 / 300 / 400 shares)

Both: last 60 s until the end, no Coinbase jump guard (the lock gate replaces it), 180 ms order / cancel latency
(post-only ack p90), pessimistic queue (cancels at a level never move us up), $5,000 paper bankroll. Paper only.
Then start them:  sudo systemctl enable --now pmtool-bot@late-fav-v2 pmtool-bot@late-fav-v2-ladder
"""
import os
import sys

from backend.bots import registry

V2 = {"gate": "lock", "lock_z": 5.0, "lock_usd": 3.0, "lock_cancel_z": 3.0, "lock_min_mid": 0.90, "lock_feed_ms": 6000.0,
      "window_s": 60.0, "stop_before_end_s": 0.0, "jump_bps": 1e9, "latency_ms": 180.0, "queue_cancel_credit": False,
      "max_window_cost": 1000.0, "bankroll": 5000.0, "windows": [5]}
BOTS = {
    "late-fav-v2": ("Late favourite v2 (lock, 99c)", {**V2, "prices": [0.99], "sizes": [1000]},
                    "v2: bids 0.99 only on the side whose final TWAP is locked (Chainlink ticks [end-62, end-3] seen so far "
                    "+ spot projected as a random walk: z >= 5 and >= $3 past the strike, market mid >= 0.90, Chainlink "
                    "alive; pulled below z 3). 1000 shares, last 60 s, pessimistic queue, 180 ms latency. Research: "
                    "research/own_bots_2026-10-27 (88 h, two periods, no losing window)."),
    "late-fav-v2-ladder": ("Late favourite v2 ladder (lock, 97-99c)",
                           {**V2, "prices": [0.97, 0.98, 0.99], "sizes": [300, 300, 400]},
                           "v2 ladder: the late-fav-v2 lock gate with bids at 0.97 / 0.98 / 0.99 (300 / 300 / 400 "
                           "shares): once locked, a sweep through 0.99 is a holder cashing out, not a reversal."),
    "late-fav-v3": ("Late favourite v3 (lock z3 from 120 s, 99c)",
                    {**V2, "prices": [0.99], "sizes": [1000], "window_s": 120.0, "lock_z": 3.0, "lock_cancel_z": 2.0},
                    "v3: v2's lock gate armed earlier (from 120 s before the end, z >= 3, pulled below z 2, still >= $3) "
                    "so the bid is in the 0.99 queue before it fills up. Research (pessimistic queue, 1000 sh): period A "
                    "+$29.2/h with no losing window, period B +$18.0/h with one partial loss (-$36)."),
    "late-fav-v3-safe": ("Late favourite v3 safe (lock z5 from 120 s, 99c)",
                         {**V2, "prices": [0.99], "sizes": [1000], "window_s": 120.0, "lock_z": 5.0, "lock_cancel_z": 3.0},
                         "v3 safe: from 120 s before the end at v2's z >= 5. Research: A +$9.9/h, B +$8.6/h, no losing "
                         "window in either period."),
    "late-fav-v3-big": ("Late favourite v3 big (lock z3 from 120 s, 99c x5000)",
                        {**V2, "prices": [0.99], "sizes": [5000], "window_s": 120.0, "lock_z": 3.0, "lock_cancel_z": 2.0,
                         "max_window_cost": 5000.0, "bankroll": 20000.0},
                        "v3 with 5000 shares: a bigger bid takes a bigger share of the 0.99 sellers. Research, 5m windows, "
                        "pessimistic queue: A +$85/h, B +$47/h, no losing window; ~$2.5k per filled window."),
}


def main():
    if (sys.argv[1] if len(sys.argv) > 1 else "") != "create":
        sys.exit(__doc__)
    have = {b["id"] for b in registry.list_bots()}
    for bid, (name, cfg, note) in BOTS.items():
        if bid in have:
            print(bid, "exists already: left as it is")
            continue
        b = registry.create_bot(name, "late_fav", {**registry.default_config("late_fav"), **cfg}, mode="paper", bot_id=bid)
        registry.add_change(b["id"], note, "settings")
        os.makedirs(f"data/bots/{b['id']}", exist_ok=True)   # systemd opens bot.log here before its own mkdir runs
        print("created", b["id"], "mode", b["mode"])


if __name__ == "__main__":
    main()

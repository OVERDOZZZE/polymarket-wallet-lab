"""The London imbalance maker (LONDON_PLAN.md Phase 4), run on the server from ~/app:

  .venv/bin/python -m scripts.imb_london_live create [variant]  # bot imb-london: imb-maker-10's size (B 5, 1 level,
                                                                # cap 6, $6 hard spend cap per window), $10 bankroll,
                                                                # live loss limit $8; paper mode + dry run (sends nothing)
  .venv/bin/python -m scripts.imb_london_live dry       # paper mode + dry run: live order flow with simulated acks
  .venv/bin/python -m scripts.imb_london_live arm       # mode = live, dry run off: REAL orders from its next start
  .venv/bin/python -m scripts.imb_london_live disarm    # mode = paper, dry run off (from its next start)
  .venv/bin/python -m scripts.imb_london_live set value0  # apply a variant to the existing bot's config
  --bot imb-london-live (any command): work on that bot instead of imb-london

variant: base (spike 0.5 bps), guard03 (0.3 bps, pull 2 s), fair1 / fair05 (fair-value guard 1 c / 0.5 c, pull 2 s),
value0 (LONDON_PLAN 11-02: always join + value guard 0 c, the replay's 10j0-v0 = imb-maker-10's size, 0.30-0.70,
inv / spend cap 6, $5 live loss limit).
The mode is read when the bot starts: sudo systemctl restart pmtool-bot@imb-london afterwards (first time:
bash deploy/install.sh imb-london). The kill switch (live_paused) works at once without a restart.
"""
import sys

from backend.bots import control, registry

BOT, SRC = "imb-london", "imb-maker-10"
if "--bot" in sys.argv:
    i = sys.argv.index("--bot")
    BOT = sys.argv[i + 1]
    del sys.argv[i:i + 2]
VARIANTS = {"base": {}, "guard03": {"spike_bps": 0.3, "pull_ms": 2000.0},
            "fair1": {"guard_mode": "fair", "fair_thr": 0.01, "pull_ms": 2000.0},
            "fair05": {"guard_mode": "fair", "fair_thr": 0.005, "pull_ms": 2000.0},
            "value0": {"guard_mode": "bps", "spike_bps": 0.5, "lookback_ms": 1000.0, "pull_ms": 1000.0, "imb_join": 0.0,
                       "value_guard": True, "value_cut": 0.0, "min_price": 0.30, "max_price": 0.70, "inv_cap": 6.0,
                       "spend_cap_usd": 6.0, "spend_cap_hard": True, "exposure_cap": True, "n_levels": 1, "B": 5.0,
                       "live_loss_limit_usd": 5.0}}


def _set(mode, dry, note):
    control.save_config(BOT, {"live_dry_run": dry})
    registry.update_bot(BOT, mode=mode)
    registry.add_change(BOT, note, "settings")
    print(BOT, "mode", mode, "dry run", dry, "- restart the bot for it to take effect")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create":
        var = sys.argv[2] if len(sys.argv) > 2 else "base"
        cfg = {**registry.get_bot(SRC)["config"], **VARIANTS[var],
               "bankroll": 10.0, "spend_cap_usd": 6.0, "spend_cap_hard": True, "inv_cap": 6.0, "B": 5.0,
               "n_levels": 1, "latency_ms": 30.0, "cancel_latency_ms": 23.0, "live_loss_limit_usd": 8.0,
               "live_paused": False, "live_merge": True, "live_dry_run": True}
        b = registry.create_bot(f"Imbalance maker London ({var})", "imb_maker", cfg, mode="paper", bot_id=BOT)
        registry.add_change(b["id"], f"LONDON_PLAN Phase 4: {SRC}'s size with variant {var} {VARIANTS[var]}; $10 bankroll, "
                                     "live loss limit $8 (the user's $10 overnight budget), $6 hard spend cap per "
                                     "window. Starts in dry run (nothing sent).", "settings")
        print("created", b["id"], "variant", var, "mode", b["mode"], "dry run")
    elif cmd == "set":
        var = sys.argv[2]
        control.save_config(BOT, VARIANTS[var])
        registry.add_change(BOT, f"Variant {var}: {VARIANTS[var]}", "settings")
        print(BOT, "config set to variant", var, "- restart the bot for it to take effect")
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

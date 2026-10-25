# Imbalance maker

Code: `backend/bots/strategies/imb_maker.py` · Strategy id: `imb_maker`

## What it does

BTC 5m: CrouchPredictions' ladders on both tokens (13 post-only bids 1c apart, sized floor(60 / (1 - |p - 0.5|))).
Each ladder's top joins the token's best bid only when the queue there is at least 40 % of the queue at the token's
two best prices, else it sits 2 ticks back. A 0.5 bps Binance futures move within 1 s pulls the threatened side for
1 s. Quotes from start + 5 s to end - 25 s, net share cap 300, holds to settlement.

Spec and reference code from the study: [BOT_SPEC.md](BOT_SPEC.md), [strategy.py](strategy.py).
`scripts/check_imb_maker.py` checks the port against both (random inputs vs strategy.py, the spec's cases A-G, the
clock and the guard case).

Per-window logs (orders, fills, guard triggers, decisions) for comparing with the study's simulator:
`data/bots/<id>/windows/<UTC day>/<slug>.json.gz`, see the module docstring for the columns.

## Versions

| Bot id | Created | Where | Mode | Status | Summary |
|---|---|---|---|---|---|
| `imb-maker` | 2026-10-25 | Dublin | paper | running | spec defaults (B 60, $5k) |
| `imb-maker-300` | 2026-10-25 | Dublin | paper | running | spec's first live-test sizing (B 5, cap 25, $300) |

### imb-maker
* Created: 2026-10-25 14:23Z · Where: Dublin · Mode: paper · Status: running (systemd pmtool-bot@imb-maker)
* Config: the strategy defaults = BOT_SPEC.md's final configuration
* Why: phase 1 of BOT_SPEC.md: paper fills to compare with the study's simulator before any live test
* Results:
* Verdict / next:

### imb-maker-300
* Created: 2026-10-25 · Where: Dublin · Mode: paper · Status: running (systemd pmtool-bot@imb-maker-300)
* Config: imb-maker's with bankroll 300, B 5 (5-8 shares per order), inv_cap 25 (BOT_SPEC.md's first live-test
  sizing; its ~$100 per-window spend cap is not implemented: imb_maker ignores max_window_cost)
* Why: paper preview of what the small live test would trade, ~1/12 of imb-maker's size
* Results:
* Verdict / next:

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
| `imb-maker-live` | 2026-10-25 | Dublin | dry run, then live | | spec phase 2: B 5, cap 25, $100/window, $60 loss limit |

### imb-maker
* Created: 2026-10-25 14:23Z · Where: Dublin · Mode: paper · Status: running (systemd pmtool-bot@imb-maker)
* Config: the strategy defaults = BOT_SPEC.md's final configuration
* Why: phase 1 of BOT_SPEC.md: paper fills to compare with the study's simulator before any live test
* Results:
* Verdict / next:

### imb-maker-300
* Created: 2026-10-25 · Where: Dublin · Mode: paper · Status: running (systemd pmtool-bot@imb-maker-300)
* Config: imb-maker's with bankroll 300, B 5 (5-8 shares per order), inv_cap 25 (BOT_SPEC.md's first live-test
  sizing; its ~$100 per-window spend cap is `spend_cap_usd`, left off here so this bot stays as it started)
* Why: paper preview of what the small live test would trade, ~1/12 of imb-maker's size
* Results:
* Verdict / next:

### imb-maker-live
* Created: 2026-10-25 · Where: Dublin · Mode: dry run (nothing sent) until the wallet is funded, then live
* Config: imb-maker-300's (B 5, inv_cap 25) + spend_cap_usd 100, live_loss_limit_usd 60, live_merge on
* Why: BOT_SPEC.md phase 2, first live test; paper twin in the same process, `imb-maker-300` next to it
* Setup: `scripts/imb_maker_live.py create | dry | arm | disarm`, then restart its unit; kill switch in Settings -> Live
* 2026-10-25 ~21:35Z: imb_join 1.01 (never joins the best bid), n_levels 13, live_loss_limit_usd 140, reconcile /
  order budget fixes. Live fills marked out -4.2 c/share at 60 s before, paper twin +0.3 c.
* 2026-10-25 (v3): quote only 15-85c (`min_price 0.15`, `max_price 0.85`; fills at <= 15c / >= 85c lost ~8.8 c/share),
  `n_levels 7` (one taker swept many levels: net hit -54 vs cap 25), cancels on their own gateway pool `cpool`
  (most losses were fills landing while our cancel was in flight, ~285 ms p50).
* Results:
* Verdict / next:

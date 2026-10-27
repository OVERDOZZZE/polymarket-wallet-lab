# Stale-quote taker

Code: `backend/bots/strategies/stale_taker.py` · Strategy id: `stale_taker` · Check: `python -m scripts.check_stale_taker`

## What it does

BTC 5m, paper only. When Binance USD-M futures moves at least `spike_bps` within 1 s, it FAK-buys the token the
move favours (up: Up, down: Down) at the ask + 2 ticks, 10 shares, before the makers' stale quotes reprice; at
most 10 orders per window, 2 s apart, limit 0.08-0.92, from start + 5 s to end - 30 s. Held to settlement.

Why: the study in `Polymarket Trader Analysis` (LIVE_NOTES.md sections 4-13, `reports/TAKER_SPEC.md`) found
that every maker version, CrouchPredictions' own orders included, loses at the order speed we have from Dublin,
while the takers who hit the makers' stale quotes after Binance futures moves win:
* makers leave quotes for seconds after a move (Crouch: 65 % of his threatened orders still rest 1 s later);
* backtest at Polymarket's real taker latency (fast-value-live's FAK orders: p50 ~0.7 s; Polymarket holds
  marketable orders), 10-22..25: 1 bps +2.9 c/share after fees, 2 bps +5.1 c/share, positive on every day;
* the fill rule (the book at the order's match time) matched fast-value-live's 264 real orders on 96.6 %.

Paper engine: fast_player's (FAK fills against the book as it stood at the match time, our own paper takes stay
taken for 3 s, fee taken in shares, Chainlink settlement). Each order's latency is drawn from fast-value-live's
real FAK latencies (`LATENCY_Q`, p10 247 / p50 713 / p90 2,529 ms). Mode 'live' is ignored: no gateway, nothing
is ever sent.

Offline replay of the real code, Dublin recordings 2026-10-24 12-18Z (62 windows, $1,000 bankroll, 10 shares):
1 bps +$123.6 (cost $1,504, 323 fills); 2 bps +$47.9 (cost $727, 169 fills).

## Versions

| Bot id | Created | Where | Mode | Status | Summary |
|---|---|---|---|---|---|
| `stale-taker-1bps` | 2026-10-26 | Dublin | paper | running | trigger 1 bps, 10 shares, $1,000 |
| `stale-taker-2bps` | 2026-10-26 | Dublin | paper | running | trigger 2 bps, 10 shares, $1,000 |

### stale-taker-1bps — trigger 1 bps
* Created: 2026-10-26 · Where: Dublin · Mode: paper · Status: running (systemd pmtool-bot@stale-taker-1bps)
* Config: strategy defaults (spike_bps 1.0, shares 10, slip_ticks 2, bankroll $1,000)
* Why: TAKER_SPEC.md step 1 (paper); more trades, smaller edge per share (backtest ~2,000 triggers/day)
* Results:
* Verdict / next:

### stale-taker-2bps — trigger 2 bps
* Created: 2026-10-26 · Where: Dublin · Mode: paper · Status: running (systemd pmtool-bot@stale-taker-2bps)
* Config: spike_bps 2.0, otherwise the defaults
* Why: TAKER_SPEC.md step 1 (paper); fewer trades (~570/day), bigger edge per share
* Results:
* Verdict / next:

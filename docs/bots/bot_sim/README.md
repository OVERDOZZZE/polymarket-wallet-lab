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
| `imb-maker-live` | 2026-10-25 | Dublin | dry run, then live | archived | spec phase 2: B 5, cap 25, $100/window, $60 loss limit |
| `imb-maker-10` | 2026-10-27 | Dublin | live | paused | $10 measurement of v4 (live_fast): 1 level a side, 30-70c, cap 6, $6/window, $5 loss limit |

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
* 2026-10-26 (v4, fast): stopped since 2026-10-25 21:59Z after -$62.7. Study (Polymarket Trader Analysis,
  LIVE_NOTES.md 1-14): the losses were execution speed (order on the book p50 121 / p90 937 ms; even Crouch's own
  orders lose at that speed). New fast order path `live_fast` (trading/fastorders.py): order / cancel on the book
  ~15-20 ms, cancels in flight. Settings: the spec's join rule again (`imb_join 0.4`, 13 levels, 10-99c) + new
  `exposure_cap` (resting + net <= inv_cap 25, ~4 orders a side). Backtest at the measured speed, 454 windows:
  +$0.34 ± 0.29 / window (settle + rebate), 3 of 4 days positive; at twice the delay -$0.6..-0.9.
* Results:
* Verdict / next:

### imb-maker-10
* Created: 2026-10-27 00:36Z · Where: Dublin · Mode: live since 00:44Z (dry run before) · Status: paused 00:47Z
* Config: imb-maker-300's + n_levels 1, min/max_price 0.30/0.70, inv_cap 6 (exposure_cap), spend_cap_usd 6,
  live_loss_limit_usd 5, bankroll 10, live_fast
* Why: first real-money test of the fast order path; measure order / cancel speed and top-of-book fill quality vs
  the paper twin with at most ~$5 at risk
* Results (first window, ~70 s of quoting, 179 orders): post round trip p10 43 / p50 357 / p90 2986 ms (up to 9 s),
  37 `TransportError: Server disconnected` in bursts (18.9 s, 28.4 s, 35-39 s), most orders cancelled before or right
  after resting (1 c re-pricing churn, ~2.5 posts/s). 4 fills (5 Up, 10 Down, $6.25). The 10-26 probe's ~15-20 ms
  holds for the first orders on an idle connection only.
* Verdict / next: paused; find why the HTTP/2 connection degrades and drops under this order churn before resuming.
* 2026-10-27 01:00-01:20Z, why it was slow (scripts/delay_probe.py, 0.02 orders that cannot fill, tcpdump):
  - not our side: the same httpx/HTTP2 client with 64 threads at the bot's rate stays at 26 ms p50 / 76 ms max (public
    endpoints); no TCP retransmits; CPU 80-90 % idle; packet timestamps = httpx timestamps (0-1 ms)
  - not Polymarket's speed or limits: on the open window posts / cancels answer in 34 ms p50 (one at a time, bursts of
    10, the bot's 3/s pattern), tier standard, bucket never below 50/60, no warnings; the taker delay (itode, 150 ms)
    is for crossing orders only
  - the cause: when our cancel reaches Polymarket before it has answered the post, it can hold that post's answer:
    on the wire the post went out at 0.4 ms, TCP ack 2 ms, then nothing until 621 / 1835 ms ("Already canceled"),
    while the cancel on a second connection was answered in 31 / 25 ms (and an unrelated request on the post's own
    connection in 22 ms). Only on the open window (next window: 46 ms max). In the live run 129 of 179 posts were
    cancelled in flight (top price flickering 1 c, cancels 3-50 ms after the post): held answers piled up, the
    16 cancel threads filled, cancels left 1-4 s late, connections dropped ("Server disconnected")
  - and a bug: posts lost with a dropped connection were marked rejected although they rested; the reconcile skipped
    them (id known), so they rested unmanaged: 3 of the 4 fills (10 of 15 shares)
* Fixes (v5): live_requote_wait_ack (re-priced orders cancelled once answered; urgent cancels still in flight; the
  side places nothing new meanwhile), cancels on their own connection + resend on a drop, 32 cancel threads, posts
  lost in transit kept as live/unconfirmed (cancelled by id, checked after 0.3 s), reconcile re-adopts rejected-but-open
  orders, live_max_inflight, rate-limit headers in state `live.rate_limit`. Checked by scripts/check_imb_maker.py.
* 2026-10-27 01:36Z v5 live (live_max_inflight 4). First 20 min: 378 posts, 0 exceptions / lost posts / cancel
  errors, 11 cancelled in flight (urgent only, was 129 of 179), post answer p50 75 / p90 381 ms (was 357 / 2986), cancel
  p50 83 ms, rate limit min 58/60, no warnings. 3 windows settled, 2 won, live realized +$3.91. Live bought much more
  than its paper twin (window 1793065200: 31 vs 6 shares, alternating Up/Down past the $6 spend cap, which only
  blocks the heavier side; net stayed within 6). 78 posts refused for balance ($10 is tight while pairs wait to merge).
* What is left of the delay is Polymarket's: a slow post now reaches the book late and is answered ~10 ms after it is
  on the book (our machine sends within 1 ms of signing: packet capture). (a) Exchange-wide pauses: every market's
  book feed stops at once (exchange timestamps), 23-32 pauses >= 250 ms and 5-10 >= 1 s per 5 m window, up to 2.25 s;
  the four slowest posts (2.0-2.4 s) waited through a 2.0 s pause seen in the 15 m market too. (b) Some 0.7-1.4 s waits
  while the market kept publishing: an intake queue we cannot see from outside.
* 2026-10-27 ~02:10Z (v6): spend_cap_hard on (past $6 the lighter side only until flat once, then nothing: the cap
  now bounds a window's spend to ~$6 + one order), live_stall_ms 300 (no new posts while the market's feed is silent
  or a post of ours waits > 300 ms; cancels still go; the paper twin has no such pause).

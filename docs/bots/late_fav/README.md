# Late-favourite maker

Code: `backend/bots/strategies/late_fav.py` · Strategy id: `late_fav`

## What it does

BTC 5m: in a window's last 2 minutes, resting 97-99c bids on the near-certain side, held to settlement; all bids pulled on a fast BTC move.

Background: `reports/ULTIMATE_REPORT.md` section 6, `reports/ult_favourite.txt`.

## Versions

| Bot id | Created | Where | Mode | Status | Summary |
|---|---|---|---|---|---|
| `late-fav` | 2026-10-22 | Dublin | paper | running | v1: 95-98c bids when the book mid >= 0.96 and the model >= 0.95, last 120 s. Paper +$520.51 on 692 windows to 10-27 (17 losing windows, worst -$96) |
| `late-fav-v2` | 2026-10-27 | Dublin | paper | running | v2: 99c only, 1000 shares, gate "lock" (final TWAP locked, z 5), last 60 s, pessimistic queue |
| `late-fav-v2-ladder` | 2026-10-27 | Dublin | paper | running | v2 gate with 97/98/99c bids (300/300/400 shares) |
| `late-fav-v3` | 2026-10-27 | Dublin | paper | running | v3: armed from 120 s at z 3 (pulled below z 2), 99c x 1000 |
| `late-fav-v3-safe` | 2026-10-27 | Dublin | paper | running | v3 timing at v2's z 5 |
| `late-fav-v3-big` | 2026-10-27 | Dublin | paper | running | v3 with 5000 shares ($20k paper bankroll) |

### late-fav

### v2: the final-TWAP lock (2026-10-27)

A window settles on the mean of the Chainlink ticks observed in [end - 62, end - 3] (`markets.twap_secs`), so in its
last minute most of the final price is already known. v2 bids a side only while that final TWAP is locked for it:
the ticks seen so far plus spot for the rest, projected as a random walk, at least `lock_z` (5) sd and `lock_usd`
($3) past the strike; the market agrees (`lock_min_mid` 0.90, best bid when there is no ask); Chainlink not silent.
It pulls the bids when z drops below `lock_cancel_z` (3). That replaces v1's book-mid / model / Coinbase-jump checks.

Why: the best bid of a late favourite is almost always 0.99 already, so v1's 95-98c bids fill mostly when a reversal
sweeps the book. On 88 h of recordings (two periods; research/own_bots_2026-10-27) under a pessimistic queue
(only trades move us up):

| rules | period A, 64 h | period B, 24.6 h |
|---|---|---|
| v1-like (95-98c, last 120 s) | -$611, 20 losing windows | -$74, 4 losing windows |
| v2, 99c, 1000 sh | +$5.6/h, 0 losing windows | +$4.1/h, 0 losing windows |
| v2 looser (z 4, from 90 s) | +$16.2/h, 0 losing | +$10.0/h, 1 partial loss (-$36) |

v2 is limited by queue position at 0.99 (30-50k shares often wait there), not by risk. Its tail risk is the open
question the paper run has to answer: one full loss (-$990) costs 100-250 hours of profit.

### v3: arm earlier, bid bigger (2026-10-27)

On the first live-paper windows v2 armed but never filled: 5.9-16k shares already waited at 0.99 when it arrived and
0-1.1k traded after. Arming earlier puts the bid in the queue before it fills up. Research sweep, 5m windows,
pessimistic queue, 0.5 s cancels (start = seconds before the end the gate may arm):

| start, z | size | period A, 64 h | period B, 24.6 h | losing windows |
|---|---|---|---|---|
| 60 s, z 5 (v2) | 1000 | +$5.6/h | +$4.5/h | 0 |
| 120 s, z 5 (v3-safe) | 1000 | +$9.9/h | +$8.6/h | 0 |
| 120 s, z 3 (v3) | 1000 | +$29.2/h | +$16.5/h | 0 |
| 120 s, z 3 (v3-big) | 5000 | +$85.5/h | +$46.9/h | 0 |
| 120 s, z 3 | 10000 | +$101.9/h | +$55.0/h | 0 |
| 180 s, z 3 | 1000 | +$19.3/h | +$20.9/h | A: one full loss (-$990) |

15m windows were worse (period B: +$1.5/h and the one partial loss), so all bots trade 5m only.


### v3 fix: Chainlink's own volatility (2026-10-27 20:29Z)

The first v3 windows armed late (e.g. 51 s before the end, behind 8.7k shares) because the gate used the engine's
Coinbase volatility (with its 0.4 bps floor): Chainlink's 1 s moves are ~0.6x Coinbase's (median $2.2 vs $3.6 a
second), so the projection's sd was ~1.5x too wide. `lock_vol = "chainlink"` (default) uses the sd of Chainlink's own
1 s ticks over 300 s, as the research did. Engine replay (late_fav as deployed, pessimistic queue, 180 ms latency),
v3 at 1000 shares on the old logger's 64 h (2026-10-19 18:00 - 10-22 10:00): **505 windows traded, +$4,383.61
(+$68.49/h), no losing window**; on 12 h of it the engine-vol version made +$14/h vs +$74/h. All lock-gate bots run
with it since 20:29Z; in its first 10 minutes live-paper v3 filled its whole 1000-share bid (v3-big 2,576 shares).

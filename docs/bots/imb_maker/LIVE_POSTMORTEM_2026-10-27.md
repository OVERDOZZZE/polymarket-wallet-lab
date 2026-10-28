# imb-maker-10 live post-mortem (2026-10-27, 00:45-08:05Z)

Analysis only: nothing was placed, armed, cancelled or restarted, and no file on the server was changed. Sources:
server DB (`live_orders`, `live_trades`, `bot_fills`, `bot_windows`, `bot_changes`), the bot's window logs
(`data/bots/imb-maker-10/windows/2026-10-27/*.json.gz`: paper twin orders/fills/decisions + live orders/fills),
the server's recordings 00:40-08:15Z (book, prints with `transaction_hash`, Binance futures, Coinbase), Polymarket's
data API (wallet trades/activity) and Gamma (`/events?slug=`, official winners, all 63 windows `resolved`).
Rebates are estimated as `shares x 0.07 x p(1-p) x 0.2`, scaled x1.066: on 10-25 that formula gave $4.55 and the
wallet was paid $4.8468 (MAKER_REBATE 10-26 00:45Z). The 10-27 rebate has not been paid yet.

Run timeline (`bot_changes`): armed 00:42Z; paused 00:46Z after the first window (post p50 360 ms / p90 3.0 s,
37 transport errors); v5 (wait-for-ack requotes, separate cancel connection) 01:36Z; v6 (hard spend cap, 300 ms
stall guard) 02:03Z; the loss limit was hit twice: raised 5 -> 10.5 at 04:10Z and 10.5 -> 16.4 at 05:04Z (restart);
stopped by the user 08:08Z.

## Answer in one paragraph

The bot lost because **it was filled almost only when the market moved through its quote**. 167 of the 246 logged live
fills (68%) landed on a price the strategy no longer wanted at the moment of the match, and 153 fills (728 of 1,186
shares, -$49.89) matched after our cancel for that order had already been sent. The fills on quotes the strategy
still wanted made +$38.48. The paper twin only gets "stale" fills inside its assumed 30 ms cancel window. Take out the
live fills on quotes that had been unwanted for more than 35 ms, and live becomes +$25.11, about the twin's +$27.69 on
the same windows. But that speed is not reachable. In the costliest group (43 fills, -$53.15) the cancel left within
5 ms of the decision and still lost, because Polymarket answered those cancels in a median 112 ms (56 ms in calm
moments). That is consistent with every maker cancelling at once when the book moves, while the held takers match
whatever is left. BTC was not the
trigger: Binance futures moved +0.13 bps against us in the second before those fills (noise). The token's own mid
had already fallen 1.35 c. Paper also wins for a second reason: its queue model puts it at the front of freshly
formed best bids that real orders reach only behind 100+ shares of other makers. Replaying imb-maker-10's exact config on the recordings reproduces paper (+$33.05), not live. With trade-through-only fills and the measured 60/90 ms latency it drops to -$0.70. The config differences from imb-maker-300 do not matter: every config wins under the paper model. **Verdict: structurally losing at our speed.** No code fix found here turns it positive.

## 1. Exchange truth (per window)

The ledger is rebuilt from `live_trades` (`maker_orders` entries with our order ids, all `CONFIRMED`, none `FAILED`).
It was cross-checked against Polymarket's public data API: **258 trades and 1,186.376 shares in both, same
transaction hashes, nothing missing on either side.** The bot's own in-memory ledger matched the exchange exactly:
$599.61 spent, -$8.39 realized on 59 settled windows, plus -$1.95 and -$0.40 on the two windows open at the stop.

| | value |
|---|---|
| windows quoted / with fills / won | 63 / 61 / 21 |
| fills / shares / USDC bought | 258 / 1,186.4 / $599.61 |
| **trading PnL (official winners)** | **-$10.74** (-0.91 c/share) |
| maker rebate, estimated | +$4.17 |
| **net** | **about -$6.57** |
| matched pairs (min(Up, Down) per window) | 524 pairs at an average cost of 101.50 c: -$7.87 |
| unmatched remainder (directional) | -$2.87 |

| hour (UTC) | live fills | live PnL | rebate est. | twin PnL (same windows) |
|---|---|---|---|---|
| 00 | 4 | +3.75 | 0.05 | +2.90 |
| 01 | 19 | -0.59 | 0.29 | +3.53 |
| 02 | 48 | +4.21 | 0.72 | -5.58 |
| 03 | 42 | -12.75 | 0.68 | +3.93 |
| 04 | 6 | -1.03 | 0.11 | +0.28 |
| 05 | 43 | -5.13 | 0.74 | +11.69 |
| 06 | 42 | +6.62 | 0.69 | +6.73 |
| 07 | 49 | -3.47 | 0.81 | +3.46 |
| 08 | 5 | -2.35 | 0.09 | +0.75 |

Most windows end as about 10 Up + 10 Down shares (spend cap $6, 5-share orders), so the result is mainly "price paid
per pair". Live paid 101.5 c per pair; the twin paid 98.2 c. 164 of the 254 fills with a time in the window came in
the first 60 s (-$10.59). The spend cap is usually used up early.

Per-window table (live from the exchange; twin and 300 from `bot_fills`, all scored on Gamma's winners):

| window (UTC) | winner | live fills | live Up sh / cost | live Down sh / cost | live PnL | live rebate est. | twin PnL | 300 PnL |
|---|---|---|---|---|---|---|---|---|
| 00:45 | down | 4 | 5.0 / 2.55 | 10.0 / 3.70 | +3.75 | 0.053 | +2.90 | +14.16 |
| 01:40 | up | 7 | 16.0 / 8.64 | 15.0 / 7.50 | -0.14 | 0.112 | +1.86 | -8.42 |
| 01:45 | down | 8 | 15.0 / 5.80 | 15.0 / 8.90 | +0.30 | 0.107 | -0.01 | +3.54 |
| 01:50 | down | 0 | - | - | 0.00 | 0 | +0.00 | -2.55 |
| 01:55 | up | 4 | 10.0 / 6.25 | 10.0 / 4.50 | -0.75 | 0.071 | +1.68 | -9.91 |
| 02:00 | down | 8 | 16.0 / 7.10 | 11.5 / 5.26 | -0.85 | 0.097 | +1.30 | -5.52 |
| 02:05 | up | 4 | 11.0 / 3.87 | 11.0 / 7.14 | -0.01 | 0.074 | -2.20 | +2.41 |
| 02:10 | up | 7 | 15.0 / 6.35 | 15.0 / 7.31 | +1.34 | 0.110 | -0.60 | -8.16 |
| 02:15 | down | 4 | 10.0 / 3.75 | 10.0 / 6.15 | +0.10 | 0.070 | +0.35 | +3.21 |
| 02:20 | down | 2 | 5.0 / 3.05 | 5.0 / 2.95 | -1.00 | 0.036 | -0.69 | -0.19 |
| 02:25 | down | 3 | 6.0 / 4.08 | 6.0 / 1.92 | +0.00 | 0.039 | -0.06 | -3.57 |
| 02:30 | down | 5 | 10.0 / 6.40 | 11.0 / 3.67 | +0.93 | 0.071 | +0.44 | -7.72 |
| 02:35 | down | 2 | 6.0 / 1.92 | 6.0 / 4.08 | +0.00 | 0.039 | -2.59 | +0.03 |
| 02:40 | up | 3 | 10.0 / 6.05 | 5.0 / 1.70 | +2.25 | 0.052 | +0.05 | +2.81 |
| 02:45 | up | 7 | 14.9 / 8.72 | 10.0 / 4.66 | +1.51 | 0.088 | -1.65 | +3.89 |
| 02:50 | down | 3 | 6.0 / 1.92 | 6.0 / 4.14 | -0.06 | 0.039 | -1.63 | -4.73 |
| 02:55 | down | 0 | - | - | 0.00 | 0 | +1.70 | +2.35 |
| 03:00 | down | 4 | 10.0 / 5.65 | 10.0 / 4.55 | -0.20 | 0.073 | -1.75 | -2.93 |
| 03:05 | down | 6 | 10.0 / 5.25 | 10.0 / 5.35 | -0.60 | 0.073 | +0.15 | -9.09 |
| 03:10 | down | 5 | 11.0 / 6.55 | 10.0 / 5.75 | -2.30 | 0.074 | +1.75 | -2.28 |
| 03:15 | down | 5 | 15.0 / 5.35 | 11.0 / 7.19 | -1.54 | 0.088 | +2.00 | +1.54 |
| 03:25 | up | 5 | 12.0 / 3.90 | 16.0 / 9.42 | -1.32 | 0.095 | +2.93 | +0.46 |
| 03:30 | up | 5 | 10.0 / 6.15 | 10.0 / 4.20 | -0.35 | 0.071 | +1.94 | -1.57 |
| 03:35 | up | 4 | 11.0 / 7.14 | 10.4 / 3.54 | +0.32 | 0.072 | +1.70 | +6.62 |
| 03:40 | down | 6 | 17.0 / 6.39 | 11.0 / 6.87 | -2.26 | 0.096 | -2.98 | -5.60 |
| 03:45 | down | 1 | 5.0 / 2.05 | 0.0 / 0.00 | -2.05 | 0.018 | +0.00 | +3.22 |
| 03:50 | down | 1 | 5.0 / 2.45 | 0.0 / 0.00 | -2.45 | 0.019 | -1.81 | -1.74 |
| 04:10 | up | 5 | 10.0 / 5.90 | 15.0 / 7.05 | -2.95 | 0.091 | +0.28 | -5.72 |
| 04:15 | down | 1 | 0.0 / 0.00 | 6.0 / 4.08 | +1.92 | 0.019 | +0.00 | -0.55 |
| 05:05 | down | 5 | 11.0 / 7.20 | 11.0 / 4.20 | -0.40 | 0.074 | +1.20 | +6.54 |
| 05:10 | up | 4 | 10.0 / 5.50 | 10.0 / 4.60 | -0.10 | 0.073 | -0.01 | +1.10 |
| 05:15 | up | 4 | 10.0 / 4.70 | 10.0 / 6.15 | -0.85 | 0.072 | +0.97 | +5.03 |
| 05:20 | down | 4 | 10.0 / 5.40 | 10.0 / 5.75 | -1.15 | 0.073 | +2.39 | +7.45 |
| 05:25 | down | 6 | 11.0 / 7.20 | 12.4 / 3.99 | +1.24 | 0.077 | +4.08 | -11.07 |
| 05:30 | up | 5 | 15.0 / 7.00 | 10.0 / 5.15 | +2.85 | 0.093 | +0.80 | +6.33 |
| 05:35 | up | 3 | 6.0 / 4.20 | 11.0 / 3.75 | -1.95 | 0.055 | +0.32 | +9.49 |
| 05:40 | up | 4 | 11.0 / 6.49 | 10.0 / 4.00 | +0.51 | 0.074 | -1.20 | -10.14 |
| 05:50 | down | 5 | 15.0 / 6.80 | 11.0 / 7.40 | -3.20 | 0.090 | +1.18 | -2.06 |
| 05:55 | up | 3 | 5.0 / 3.00 | 11.0 / 4.08 | -2.08 | 0.056 | +1.95 | +0.38 |
| 06:00 | down | 3 | 5.0 / 1.90 | 12.0 / 8.10 | +2.00 | 0.057 | +1.52 | +6.00 |
| 06:10 | down | 6 | 10.0 / 6.05 | 16.0 / 5.66 | +4.29 | 0.090 | +0.72 | -7.91 |
| 06:15 | down | 4 | 10.0 / 5.15 | 10.0 / 4.50 | +0.35 | 0.074 | +0.40 | +8.44 |
| 06:20 | down | 2 | 6.0 / 1.92 | 6.0 / 4.14 | -0.06 | 0.039 | +0.00 | +1.51 |
| 06:25 | up | 5 | 10.0 / 5.21 | 10.0 / 3.70 | +1.10 | 0.072 | -1.00 | +3.37 |
| 06:30 | up | 4 | 11.0 / 7.20 | 11.0 / 4.30 | -0.50 | 0.074 | +2.08 | +4.74 |
| 06:35 | down | 4 | 10.0 / 4.05 | 10.0 / 6.05 | -0.10 | 0.071 | -0.03 | +5.57 |
| 06:40 | down | 4 | 10.0 / 5.20 | 10.0 / 5.25 | -0.45 | 0.074 | +0.55 | -11.12 |
| 06:45 | down | 3 | 5.0 / 2.80 | 6.8 / 3.45 | +0.50 | 0.043 | +2.85 | +8.26 |
| 06:50 | down | 4 | 7.5 / 3.12 | 10.0 / 6.40 | +0.48 | 0.061 | +0.00 | +2.87 |
| 06:55 | up | 3 | 5.3 / 3.45 | 5.0 / 2.85 | -1.00 | 0.036 | -0.36 | +0.29 |
| 07:00 | down | 4 | 10.0 / 3.85 | 10.0 / 6.20 | -0.05 | 0.070 | -2.15 | +6.48 |
| 07:05 | up | 4 | 10.0 / 6.20 | 10.0 / 4.05 | -0.25 | 0.071 | -0.15 | -5.09 |
| 07:10 | down | 1 | 6.0 / 4.14 | 0.0 / 0.00 | -4.14 | 0.019 | +2.10 | +9.63 |
| 07:15 | up | 4 | 10.0 / 5.50 | 10.0 / 5.60 | -1.10 | 0.073 | +1.95 | +6.03 |
| 07:20 | up | 7 | 10.0 / 4.95 | 15.0 / 6.71 | -1.66 | 0.090 | +0.35 | +1.67 |
| 07:25 | up | 5 | 10.0 / 5.45 | 12.3 / 5.59 | -1.04 | 0.082 | +0.43 | -1.92 |
| 07:30 | up | 5 | 15.0 / 6.00 | 5.0 / 2.85 | +6.15 | 0.072 | +0.03 | +10.67 |
| 07:40 | down | 4 | 11.0 / 4.16 | 10.0 / 5.40 | +0.44 | 0.075 | +1.32 | -7.36 |
| 07:45 | up | 5 | 10.0 / 5.15 | 14.3 / 8.68 | -3.83 | 0.087 | -0.75 | +15.10 |
| 07:50 | up | 6 | 17.0 / 10.64 | 11.0 / 4.01 | +2.35 | 0.096 | +0.35 | +9.87 |
| 07:55 | down | 4 | 10.0 / 4.20 | 10.0 / 6.15 | -0.35 | 0.072 | -0.02 | +6.59 |
| 08:00 | up | 1 | 0.0 / 0.00 | 5.0 / 1.95 | -1.95 | 0.018 | +0.00 | -0.26 |
| 08:05 | down | 4 | 10.0 / 6.40 | 10.0 / 4.00 | -0.40 | 0.070 | +0.75 | +4.01 |

Minor ledger bug found: one 5-share fill (06:53:19Z, Down 0.63) has `live_orders.filled = 0`. The fill arrived before
the post's response, and `_fast_record`'s deferred `INSERT OR REPLACE` (on `gw.bg`) then overwrote the earlier
`UPDATE ... filled`. The in-memory ledger was right; a restart (`_load_live_maker`) would undercount this.

## 2. Live vs the paper twin, order by order

Twin and live take the same decisions from the same book, guard and clock, but each uses its own inventory and spend.
Matching is by (window, side, price) plus time: a fill counts as matched within +-2 s. An order counts as resting when
its estimated exchange interval covers the moment: live from send + 25 ms to cancel-send + 25 ms; twin from
`live_ms` to `cancel_eff_ms`. Match time of a live fill = earliest of (book-level shrink, print, our fill event).

On the 57-63 windows both traded: live -$10.74 on 258 fills; twin **+$27.69** on 184 fills; imb-maker-300 +$54.49.

**Live fills: what the twin did at that moment**

| category | fills | shares | PnL | c/share |
|---|---|---|---|---|
| twin filled too (+-2 s) | 11 | 56.0 | +0.46 | +0.8 |
| twin resting at the same price, not filled (its queue said "not yet") | 58 | 263.7 | -4.86 | -1.8 |
| **twin had already cancelled that price (live order stale)** | **86** | **424.2** | **-54.32** | **-12.8** |
| twin not quoting that price (live inventory / spend state differed) | 99 | 422.5 | +48.38 | +11.5 |

**Twin fills: what live did at that moment**

| category | fills | shares | PnL | c/share |
|---|---|---|---|---|
| live filled too | 13 | 56.0 | +5.80 | +10.4 |
| **live resting at the same price, never filled** | **43** | **203.6** | **+19.41** | **+9.5** |
| no live order: rejected for balance / post-only | 5 | 25.0 | -4.80 | |
| no live order: live had one there within 3 s (in flight / just cancelled) | 10 | 41.9 | +4.03 | |
| no live order near (live cap / spend cap / stall guard / loss-limit halt) | 107 | 479.1 | -0.47 | -0.1 |

Conclusions:
* Only 11-13 fills are common to both. **The two books of fills are mostly different events.**
* Paper-only good fills: 43 twin fills (+$19.41) happened while the live order rested at the same price and never
  filled. The real queue was longer than the model's (section 5).
* Live-only bad fills: 86 live fills (-$54.32) were on prices the twin had already cancelled.

**The same split by the strategy's own decision log** (was the filled price still the desired top of that side at the
match? the twin's inventory codes are ignored, so this is market-only):

| at the match the strategy... | fills | shares | PnL | c/share | unwanted for (p50) | cancel already sent |
|---|---|---|---|---|---|---|
| still wanted this quote | 79 | 348.5 | **+38.48** | +11.0 | - | 10 |
| had re-priced it: best bid fell below us (market moving through us) | 57 | 264.0 | **-38.75** | -14.7 | 73 ms | |
| had pulled it: Binance guard trigger | 7 | 32.0 | **-16.34** | -51.1 | 102 ms | |
| had re-priced it: best bid moved above us | 6 | 33.0 | -4.91 | -14.9 | 83 ms | |
| wanted 2 ticks back (queue share fell below 0.4) | 97 | 461.3 | +12.03 | +2.6 | 92 ms | |
| no decision log yet / window log missing | 12 | 47.5 | -1.25 | | | |

The losing fills are the 70 "moved through / guard" fills: -$60.00, -18.2 c/share. Twin fills on prices it no longer
wanted: 37, all inside its 30 ms window, and they made +$6.18.

## 3. Markout and adverse selection

Markout = the filled token's mid (book (bid+ask)/2, exchange time) h seconds after the match, minus the fill price, in
c/share (settlement value past the window's end). "Against" = BTC move in the direction that hurts our token, in bps.

| fills | n | shares | PnL c/sh | 1 s | 5 s | 30 s | 60 s | BTC fut 1 s before | BTC fut 5 s before | price |
|---|---|---|---|---|---|---|---|---|---|---|
| **LIVE all** | 258 | 1186 | -0.91 | **-1.98** | -1.56 | -0.96 | +0.87 | +0.09 | -0.01 | 0.505 |
| LIVE cross-token (taker bought the other token) | 244 | 1171 | -1.13 | -2.02 | -1.61 | -0.93 | +0.81 | +0.09 | 0.00 | |
| LIVE same-token (taker sold our token) | 14 | 15 | +16.2 | +1.15 | +1.80 | -3.68 | +5.81 | | | |
| LIVE sweeps (taker hit > 1 price) | 110 | 556 | -1.22 | -2.10 | -0.53 | +0.92 | +2.64 | | | |
| LIVE cancel already sent before match | 209* | 1001 | -2.74 | -2.17 | -1.76 | -0.79 | +0.63 | | | |
| LIVE price >= 0.60 | 75 | 359 | +1.15 | -2.26 | -1.94 | -2.53 | -1.31 | | | 0.643 |
| LIVE 0.41-0.59 | 114 | 505 | -2.44 | -1.50 | -1.71 | -0.95 | +0.31 | | | 0.505 |
| LIVE <= 0.40 | 69 | 322 | -0.80 | -2.41 | -0.91 | +0.79 | +4.20 | | | 0.354 |
| **TWIN (live windows)** | 176 | 796 | +3.01 | **+0.52** | -0.40 | -0.37 | +1.63 | -0.10 | -0.31 | 0.498 |
| **imb-maker-300 (00:45-08:10)** | 1976 | 9595 | +0.71 | **+0.47** | +0.58 | +2.17 | +3.72 | -0.10 | -0.32 | 0.517 |

\* by print time; 153 by the earlier book-shrink time.

Before the fill (c of the token's mid; bps of BTC; + = against our token):

| fills | token mid change, prior 1 s | prior 5 s | Coinbase, prior 1 s | Binance fut, prior 1 s | Binance fut, next 1 s / 5 s |
|---|---|---|---|---|---|
| LIVE, quote still wanted | +0.11 | +0.09 | +0.10 | +0.03 | -0.01 / -0.06 |
| **LIVE, moved through / guard** | **-1.35** | -0.53 | +0.25 | +0.13 | +0.09 / +0.32 |
| TWIN | **+1.06** | +2.69 | -0.14 | -0.10 | -0.05 / -0.01 |
| imb-maker-300 | **+1.04** | +2.17 | -0.10 | -0.09 | -0.04 / -0.09 |

Conclusions:
* **Yes, it is adverse selection, and it is short-horizon:** -2.0 c/share at 1 s live vs about +0.5 in paper. That
  gap times 1,186 shares is about -$29, most of the gap to the twin.
* **It does not come from BTC.** Binance futures and Coinbase barely moved before the losing fills (0.1-0.3 bps;
  the guard's trigger is 0.5 bps). It comes from Polymarket's own flow: the token's mid had fallen 1.35 c in the
  second before. The guard pulls that still filled (7 fills, -$16.34) are the only clearly BTC-driven losses.
* 95% of fills are cross-token (a taker buying the other token mints against our bid), the same as on 10-25.
  Same-token fills are rare (14) and fine.
* Sweeps are a little worse (-1.22 c/share vs -0.62 for the rest) but are not the main cause at n_levels 1.
* Price band: fills at 0.60-0.70 were not the problem over the full run (+$4.14 on 75 fills). The 0.41-0.59 band
  lost the most (-$12.32).
* Paper fills come **after the token rose** (+1 c in 1 s): the sim joins a freshly raised best bid 30 ms after seeing
  it and fills from there. Live does not get those fills (section 5).

## 4. Did the speed fixes work?

decide = the strategy's decision; ack = the exchange's answer (our clock). Cancel decision for a live order = the
twin's cancel time of the same order (same decision; paired by side/price/decision ms).

| run | orders | decide -> ack p10 / p50 / p90 | HTTP post p50 / p90 | cancel sent -> ack p10 / p50 / p90 | decision -> cancel ack p10 / p50 / p90 |
|---|---|---|---|---|---|
| 10-25 imb-maker-live (batch path) | 3,991 | 43 / 148 / 1,209 | 70 / 515 | 33 / 95 / 553 | 35 / 185 / 1,445 |
| 10-27 v4, first window | 179 | 46 / 360 / 2,992 | 357 / 2,986 | 44 / 187 / 1,708 | 38 / 155 / 957 |
| 10-27 v5 01:36-02:03 | 468 | 31 / 86 / 469 | 82 / 464 | 33 / 87 / 364 | 52 / 166 / 774 |
| **10-27 v6 02:03-08:05** | 6,808 | 33 / **63** / 156 | 59 / 152 | 29 / **59** / 137 | 30 / **89** / 436 |
| paper twin assumption | | 30 | | 30 | 30 |

* **The fixes worked on their own terms.** v6 is about 2.3x faster at the median than 10-25 and 3-8x faster at
  p90. Fill events now reach the bot about 40 ms after the match (book shrink -> our event: p50 ~41 ms), where
  10-25 saw 0.4-2 s. The inventory-overshoot cause from 10-25 is gone.
* Clocks are aligned: our own orders appear in the book feed 20-40 ms after sending, and our cancels take effect
  20-60 ms after sending (histogram peaks). So the timings below are not a clock artefact.
* **Fills on quotes already being cancelled:**
  * at the match, 167 of 246 logged fills were on a price the strategy no longer wanted (70 of them moved through /
    guard: -$60.00);
  * 153 fills (728 shares, **-$49.89**) matched after our cancel of that very order had been sent, 69 of them 60 ms
    or more after;
  * 215 cancels came back "order can't be found - already canceled or matched".
* **Wait-for-ack is not the main cost.** Of the 70 moved-through / guard fills:

| cancel timing | fills | PnL | decision -> cancel sent (p50) | decision -> match (p50) |
|---|---|---|---|---|
| cancel sent at the decision (<= 5 ms) | 43 | **-53.15** | 0 ms | 51 ms |
| cancel sent late (post still in flight: waited for its ack) | 20 | -5.05 | 76 ms | 121 ms |
| cancel not yet sent at the match | 7 | -1.80 | 129 ms | 119 ms |

* **Cancels slow down exactly when it matters.** On those 43 prompt-cancel fills the cancel took 40 / 112 / 347 ms
  (p10 / p50 / p90) to be answered. Within 300 ms of any of our fills, cancels took p50 79 ms, and 56 ms otherwise
  (6,465 v6 cancels). Our cancel queues behind everyone else's when the book moves; the held taker is matched first.

## 5. Why paper wins: fill model, timing or config?

Replay: `backend.bots.replay.run` on the server (nice 19, nothing written to disk, winners from Gamma), recordings
00:38-08:15Z, the real configs from the `bots` table. Two engine variants were added in the driver only:
* latency `60/90`: orders live 60 ms after the decision, cancels effective 90 ms after it (v6 medians);
* fill model `tt` ("trade-through"): a resting bid fills only when a print is strictly better for the taker than
  our price. It never fills from the queue at our own level.

A third variant, 150/400 ms, starved itself: the $10 bankroll stays reserved while cancels are pending (3 fills).
It is left out.

**On the 63 windows live quoted** (00:45-08:10Z):

| run | fills | shares | PnL | c/share | 1 s markout | token mid, prior 1 s | live fills it also has (of 258) |
|---|---|---|---|---|---|---|---|
| **LIVE** | 258 | 1,186 | **-10.74** | -0.91 | -1.98 | -1.35 (stale fills) | - |
| twin (real time) | 184 | 833 | +27.69 | +3.32 | +0.52 | +1.06 | 11 |
| replay, imb-maker-10 exact config (30 ms, queue model) | 195 | 916 | +33.05 | +3.61 | +0.83 | +1.20 | 6 |
| replay, + latency 60/90 | 260 | 1,107 | +17.05 | +1.54 | +0.20 | +0.12 | 38 |
| replay, fill model tt (30 ms) | 82 | 360 | +10.92 | +3.03 | -0.89 | +0.15 | 5 |
| **replay, tt + latency 60/90** | 237 | 900 | **-0.70** | -0.08 | **-1.22** | **-1.33** | 32 |
| imb-maker-300 (real time) | 1,319 | 6,417 | +54.49 | +0.85 | +0.51 | +1.04 | |
| replay, imb-maker-300 config | 1,296 | 6,292 | +83.60 | +1.33 | +0.46 | +1.21 | |
| replay, 300 + latency 60/90 | 2,590 | 11,993 | +22.19 | +0.19 | -0.28 | -0.24 | |
| replay, 300 fill model tt | 323 | 1,616 | +46.79 | +2.90 | -0.61 | -1.01 | |

Config ablations, replay with the paper fill model, imb-maker-10's config with one change:

| change | fills | PnL | pairs PnL | directional PnL | 1 s markout |
|---|---|---|---|---|---|
| none (imb-maker-10) | 195 | +33.05 | | | +0.83 |
| inv_cap 25, no spend cap | 521 | +96.80 | -7.71 | +104.51 | +0.55 |
| n_levels 13 | 204 | +27.02 | +2.72 | +24.30 | +0.86 |
| band 0.10-0.99 | 237 | +16.04 | +6.49 | +9.55 | +1.12 |

Whole span 00:45-08:10Z (89 windows): replay 300 +$104.73 -> 60/90 ms +$4.11 -> tt +$23.44; replay 10 +$41.74
-> 60/90 ms +$19.04 -> tt + 60/90 -$1.48.

Conclusions:
* **The replay reproduces paper, not live.** imb-maker-10's exact config replays to +$33.05, against the twin's
  +$27.69 in real time (119 of 184 twin fills are in the replay; timing differs between the live engine and the
  recording). It contains only 6 of the 258 real fills.
* **It is not the config.** Under the paper fill model every config wins on these windows, imb-maker-10's own
  included. Cap 25 just takes more inventory and "wins" more. Every paper bot's profit is directional: the
  unmatched remainder lands on the winning side (300: pairs -$94, remainder +$178). Their pairs lose (300 -3.5
  c/pair, imb-maker -5.5). That remainder comes from fills right after the token rose. On 63 windows this is mostly
  the luck of one day's trends, and it does not survive a realistic fill model (tt + latency).
* **It is the fill model plus timing, and the two compound.** Either fix alone takes about half of paper's profit;
  together they take all of it (-$0.70). That variant reproduces live's signature: -1.22 c at 1 s and a falling
  token before the fill (-1.33 c), live -1.98 / -1.35.
  * The queue model is optimistic in good moments: twin fills sit at levels of median 50 shares at the decision that
    hold median 216 shares 60 ms later (54% grew by >= 20%). A real order lands behind those joiners, so 43 paper
    fills (+$19.41) happened while the live order rested at the same price and never filled.
  * The 30 ms cancel is optimistic in bad moments. Paper is never caught on a quote it wants to drop; live is,
    on 68% of its fills.
* Even the pessimistic sim is $10 better than live. It misses cancel congestion (answers in 112 ms instead of 56 ms
  during moves) and gives no fills from the queue at our own price, so it cannot be used to tune this bot either.
  It finds only 32 of the 258 real fills.

## 6. Verdict

**At our speed this maker is structurally losing. No fixable cause found here would turn it positive; the small
fixable items are listed below.**

Evidence for "structural":
1. The edge is real but small: fills on quotes the strategy still wanted made +$38.48 (79 fills, +11 c/share).
   Rebates add about 0.35 c/share (+$4.17).
2. The loss is bigger and comes from being the last quote at a level the market is moving through: -$60.00 on 70
   fills (-18 c/share). These fills are 27% of the fills and 28% of the shares.
3. Those losses are not caused by slow code. In 43 of them (-$53.15) the cancel left within 5 ms of the decision.
   Polymarket then took a median 112 ms to answer it, twice the calm-time 56 ms, because every maker cancels in the
   same moment. The held taker is matched against whoever is still there.
4. There is no earlier warning we could have used. Binance futures and Coinbase were flat before these fills, and
   the guard's 0.5 bps trigger could not fire. On markouts, none of these separated the losing fills: token mid change
   over 1 s / 5 s, queue share, price band, time in window. Settlement-PnL splits by them looked strong (e.g. share
   >= 0.4: -$23; first 60 s of the window: -$10.99) but the markouts were about equal, so they are noise at this
   sample size.
5. Under the only sim that reproduces live's markout signature (trade-through fills, 60/90 ms), imb-maker-10's
   config is breakeven before rebates (-$0.70 on the live windows, -$1.48 on all 89). imb-maker-300's config falls
   from +$104.73 to +$4.11 with latency alone. Live did $10 worse than that sim.
6. If live had cancelled every quote within 35 ms of the decision (the twin's assumption), it would have made about
   +$25.11, matching the twin's +$27.69. That 35 ms includes ~25 ms one-way to London and is not achievable from
   Dublin while everyone is cancelling. So the twin's profit is the assumed speed, not the strategy.

Smaller items. On paper the first two together cost $21.39, enough to turn this sample to about +$10.65. Neither is
removable, though. The guard pulls already went out as fast as we can cancel, and wait-for-ack replaced something
worse. Only the last three are real fixes, and they do not change PnL:

| item | evidence | cost on 10-27 | change |
|---|---|---|---|
| Guard pulls that still filled | 7 fills; pull decided p50 102 ms before the match | -$16.34 | Already as fast as the cancel path allows. Only a guard that fires earlier (none found, point 4) would help. |
| Wait-for-ack requotes | 20 stale fills where the cancel waited for the post's answer (p50 76 ms) | -$5.05 | Keep it. Cancelling posts in flight cost far more in the first window (post p90 3 s, 37 transport errors). |
| Paper fill model | sections 2 and 5 | every paper maker result | Before trusting a maker paper result, replay it with trade-through fills + measured latency. Better: a queue model where joiners within ~60 ms go ahead of us, and where cancels at a level about to be crossed come from ahead of us. |
| `live_orders.filled` race | 1 fill of 5 shares lost from its DB row | ledger rebuild on restart only | Write `filled` without the later `INSERT OR REPLACE` wiping it (upsert that keeps `filled`). |
| Small wallet | 213 `not_enough_balance` rejects (mostly before 05:04Z), 5,461 stall-guard skips | divergence from the twin | Only matters for the twin comparison. |

What would have to be true before another live test makes sense:
* a signal that moves before Polymarket's book does (the Binance guard does not, on this data); or
* a way to never be the last order at a level that is about to empty. 10-25's version always quoted 2 ticks back
  and also lost, and the paper sim cannot test this honestly (section 5); or
* a location where our cancel lands before the held taker is matched.

Otherwise this design pays about 1 c/share in adverse selection for a 0.35 c/share rebate.

## Method notes

* Match time of a live fill: the earliest of (a) the book-level shrink at our price in the market feed, (b) the
  print (`last_trade_price`, joined to our trade by `transaction_hash`: all 258 found), and (c) our first fill event.
  The print's exchange timestamp lags the shrink by a median 22 ms, and 17% of "already matched" cancel answers came
  back before it, so the print alone overstates how late fills were.
* "Wanted at the match": the twin's decision row at that moment (bb, ba, top depths, guard code) gives the desired
  top price per side; inventory / spend-cap codes are ignored because they are the twin's, not live's.
* Replay variants were defined only inside an analysis driver (nothing changed in the repo or on the server). `tt`
  replaces `ImbMakerBot._on_print`: of the candidate resting bids, only those priced strictly better than the print
  (in our token's terms) fill; the same-level queue branch is dropped.
* Sample size: 61 windows, 258 fills. Settlement PnL per bucket is noisy (each window is one coin flip), so causes
  are only claimed when the markouts (1-60 s) agree.

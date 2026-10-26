# fast_player: why live FAKs miss, and does a price allowance help? (2026-10-26)

Question: fast-value-live fills ~10% of its orders (paper twin ~95%). Would a wider allowance (`extra_pay`: cents added
to the order limit) buy more fills profitably, and what is the best setting?

Data: the Dublin server's recordings 10-24 00:00 to 10-26 05:00 UTC (`data/recordings_server`, ~35 h usable: the
recorder lost Chainlink for whole hours, fixed in recorder.py), the 158 real fast-value-live orders of that period
(`live_orders.json`), and the old logger PC's 64 h (10-19..22, coarser book) as an out-of-sample check.

## 1. How live FAKs really fill

`live_orders.ts` is written when the response is back; the order left at `ts - latency_ms`.

* Round trips of FAK (taker) orders: p10 0.3 s, median 0.9 s, p90 2.4 s, max 3.6 s. Post-only orders on the same
  account: 20-80 ms. Polymarket holds orders that take liquidity.
* At the moment the order is sent, the book still has asks at the limit for 99% of orders (median 516 shares).
* The order matches against the book **as it stands when the response comes back** (book at send + RTT reproduces
  live fill / miss on 94% of the orders, `fast_live_probe.py`).
* That moment is not random: the response comes 0-0.7 s (mode 0.1-0.2 s) after the asks at the limit vanish
  (correlation 0.84). Of the misses, 94 were asks **cancelled without trading** (makers saw the same BTC move),
  37 taken by other takers first. With the same RTTs shuffled across orders, 37% would fill instead of 12%.

So the bot's 50 ms paper fills (~95%) and even random live-like delays (~37%) are far too optimistic.

**Queue model** (fast_player `queue_max_ms`): the order waits until no ask is left at its limit, then matches a
draw from `queue_gap_samples` later, or at `queue_max_ms` if the asks stay. With 2200 ms it fills 8.6% of the
replayed orders of 10-25 18:00..10-26 05:00 (live: 7%) and agrees with live on 42 of the 44 orders both sent.

## 2. Allowance on the real live signals (`probe.json`, 153 orders with a winner, $15 each)

| allowance | fills (own RTT) | PnL | fills (delay grows while the wider price survives) | PnL |
| - | - | - | - | - |
| 0 | 11.8% | +47 | 11.8% | +47 |
| +1c | 16.3% | +100 | 11.8% | +37 |
| +2c | 18.3% | +148 | 15.7% | +65 |
| +3c | 22.2% | +111 | 20.3% | -36 |
| +5c | 40.5% | -57 | 26.8% | -50 |
| +10c | 68.0% | -175 | 52.9% | -231 |

95% bootstrap intervals are about ±$150 on every row. A limit relative to the ask (ask + x) was worse than the bot's
edge limit + x.

## 3. Replay sweeps with the queue model (35 h, $15 orders; `fast_allowance_sweep.py`, `fast_allowance_report.py`)

| variant (spike 2 bps unless said) | orders | fills | PnL | ROI |
| - | - | - | - | - |
| current: +0, cap 0.60 | 150 | 9.3% | -14 | -7% |
| +2c, cap 0.60 | 150 | 15.3% | +51 | +18% |
| +3c, cap 0.60 | 150 | 19.3% | +67 | +18% |
| +3c, cap 0.70 | 180 | 23.3% | +108 | +20% |
| +5c, cap 0.60 / 0.70 | 150 / 180 | 34% / 33% | -70 / -52 | -10% / -6% |
| +8c, cap 0.60 / 0.70 | 150 / 180 | 50% / 52% | -254 / -214 | -24% / -16% |
| 1.5 bps, +2c, cap 0.70 | 287 | 13.6% | +431 | +86% |
| 1.5 bps, +3c, cap 0.70 | 287 | 18.5% | +430 | +63% |

Not better than +3c / 0.70: min_edge 0.04 / 0.06, Binance futures in the signal, 4 entries per window, fair_mode
both / model, spike 1.0 / 1.25. PnL is lottery-like (cheap contracts): one window made +$269 in every 1.5 bps
variant; without each variant's 5 best windows all of them are negative.

Out of sample (old 64 h, coarser book, queue model): current +413, +2c +226, +3c/0.70 +208, +5c/0.70 +321
(-415 without its best 5 windows), 1.5 bps +92 / +89 / -78 (0 / 2 / 3c). The in-sample ranking does not hold.

## Conclusions

1. Live misses are structural: Polymarket's taker delay lets makers pull their quotes first. Fills come mostly when
   the move was not real. No latency gain on our side fixes this; only resting (maker) orders avoid the delay.
2. An allowance buys fill rate but not reliably profit. +2-3c roughly doubles fills (10% -> 15-23%) and was the best
   setting on the recent data and the real live signals; on the older data it earned less than none. +5c and more
   loses on the recent data.
3. 1.5 bps spikes looked best in sample and worst out of sample: not adopted.
4. Paper bots on the server, all on the queue model so they compare fairly (`scripts/fast_value_wide.py`):
   `fast-value-q` (control, current settings), `fast-value-wide` (+2c), `fast-value-wider` (+3c, cap 0.70).
   Judge them against each other after a few days, not against the 50 ms paper twin.

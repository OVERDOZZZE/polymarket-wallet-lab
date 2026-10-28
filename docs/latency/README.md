# Measured latencies: the delays every bot simulation must use

Every simulated bot so far looked good on paper and lost (or never filled) live. The cause was the same each
time: the paper fills assumed delays that don't exist on Polymarket. This folder fixes that once. All delays
were measured on 2026-10-27 on the Dublin server, where the bots trade. They are stored as
full distributions, so a simulation can draw realistic values or test best, typical and worst cases.

* **Data:** `backend/bots/latency_profile.json` (committed). Each entry has n, mean, min, p1 to p99.9, max, and
  101 quantiles for sampling.
* **Code:** `backend/bots/latency.py`, used as `Latency(site).draw / pick / fak_match_ms / maker_times`.
* **Raw measurements:** `data/latency/` (not committed). Rebuild with `scripts/latency_build.py`, see §6.

## 1. How to use it in a bot or a replay

```python
from backend.bots.latency import Latency
lat = Latency(seed=1)                      # site "dublin" (the server; the only site measured)

age = lat.draw("bot_feed.binance_fut_trade")   # when the bot learns of a Binance futures trade (ms after it)
book = lat.draw("bot_feed.polymarket_book")    # how stale the bot's Polymarket book is
t = lat.fak_match_ms(vanish_ms=v)              # when a taker order matches (see rule 3); fill against the book then
place, cancel, notice = lat.maker_times()      # post-only: exchange has it / cancel took effect / we hear of a fill
lat.pick("order.cancel.ack", "worst")          # fixed values for stress tests: best, typical, mean, worst (p99), max
```

**Rules for every simulation:**

1. **Signals arrive late.** Add `bot_feed.<source>` to each venue event before the bot may react. For the very
   best a lean client can do, use `feed.<source>`. Binance from Dublin is never under 99 ms.
2. **The bot's view of Polymarket is late too.** Add `bot_feed.polymarket_book` to every book change: 12 ms
   typical, 194 ms p99, up to 6.8 s in bursts.
3. **Taker (FAK) orders are held.** Polymarket holds orders that take liquidity, and makers pull their quotes
   during the hold. Use `fak_match_ms`. An order fills only if the asks at its limit are still on the book about
   **1.7 s after it was sent** (`order.fak.model.hold_ms`). Otherwise it matches `order.fak.queue_gap` after
   they vanish and is killed. Never fill a taker at signal time plus a fixed or random latency: those models
   predicted 37–94% fills against a real 7.7% (§5).
4. **Maker orders are not instant either.**
   * A placed order is live after `order.post_only.ack` (65 ms typical, 177 ms p90, 1.2 s p99).
   * A cancel takes effect after `order.cancel.ack` (61 ms typical, 156 ms p90, 0.9 s p99). Until then the old
     quote can still be filled.
   * The bot learns of a fill only after `order.fill_notice` (19 ms typical, but 1.6 s p90 and 8 s p99).
     Inventory caps must allow for fills the bot doesn't know about yet.
5. **Stale asks reprice fast.** After a Binance futures move of 1 bps or more, the favoured token's ask is
   unchanged after 100 ms in 81% of cases, after 250 ms in 53%, after 500 ms in 30% and after 1 s in 18%. The
   `market.*` keys hold the full distributions.
6. **Judge at "worst" before "typical".** Run every replay three times: with draws, with `pick(...,"typical")`
   and with `pick(...,"worst")`. A strategy that is only positive at typical latency is not ready for money.

## 2. Feeds: venue event → our process (ms)

`feed.*` comes from the lean probe: one process per feed, the frame stamped right after `recv()`, no
compression, JSON parsed later. `bot_feed.*` comes from 87 h of the recorder (2026-10-24 to 10-27): what the
bots really got, sharing a loop with other sockets, with deflate, on the busy server.

| Source (key suffix) | Best case p50 / p99 | As the bots see it p50 / p99 / max |
|---|---|---|
| Binance futures bookTicker (`binance_fut_book`) | 104 / 134 | not recorded |
| Binance futures aggTrade (`binance_fut_trade`) | 106 / 251 | 105 / 255 / 250,915 (one stale replay) |
| Binance spot trade (`binance_spot_trade`) | 105 / 138 | not recorded |
| Coinbase ticker (`coinbase`) | 49 / 145 | 49 / 194 / 4,864 |
| Kraken trades (`kraken_trade`) | 17 / 89 | not recorded |
| Kraken ticker (`kraken`) | 34 / 930 | 21 / 1,362 / 18,236 |
| Polymarket book (`polymarket_book`) | 12 / 168 | 12 / 194 / 6,841 |
| Chainlink via RTDS, publish → us (`chainlink`) | 282 / 470 | 267 / 444 / 2,518 |
| Chainlink, observation → us (`chainlink.observation`) | 1,349 / 2,113 | 1,321 / 2,110 |

What this means:

* **Binance aggTrade publishes trades in batches.** The publish time minus the trade time is up to 150 ms (p90
  150 ms, `venue.binance_fut_aggtrade_publish_after_trade`). **The bookTicker stream reaches the bot faster:**
  113 ms p90 against 250 ms. Bots that signal on Binance futures should use bookTicker. It runs at about 500
  msg/s, so it is heavy but fine on its own process.
* **Binance's servers are in Tokyo,** so no one in Europe sees them in under ~99 ms. One way is about RTT/2:
  ping 202–208 ms.
* **Kraken trades are the fastest BTC signal at Dublin** (17 ms), but they come rarely: the median gap is
  660 ms. Kraken ticker timestamps are not reliable for timing (p99 930 ms).
* **Chainlink, which settles the windows,** reaches us about 0.3 s after RTDS publishes it and 1.3 s after its
  observation.

**Feed gaps** (`gap.*`, `bot_gap.*`: time between messages, so a bot must handle freezes):

| | Dublin p50 | p99 | Worst seen |
|---|---|---|---|
| Polymarket book (per window socket) | 0.8 | 28 | 28.6 s |
| Binance futures aggTrade | 0.2 | 1,130 | 28.7 min (feed outage) |
| Binance spot bookTicker | 0.2 | 234 | 10.9 s |
| Coinbase | 69 | 2,298 | 18.8 s |
| Chainlink | 1,088 | 1,840 | 42.7 min (recorder lost RTDS) |

The worst hours are in `bot_feed.worst_hours`. For example, the Polymarket book p99 reached 4.0 s in hour
10-27 02 UTC, against 0.2 s overall.

## 3. Network round trips from the server (ms, p50)

TCP connect measures the venue's edge, which is Cloudflare or AWS close to us. A kept-alive HTTPS GET measures
the venue's own servers. That second number is the real distance.

| Venue | TCP (edge) | HTTPS keep-alive (servers) | WebSocket ping |
|---|---|---|---|
| Polymarket CLOB `/time` | 1.0 | **24** | 18 (market socket) |
| Polymarket Gamma | 0.5 | 4 | |
| Polymarket data-api | 1.0 | 8 | |
| Binance spot / futures REST | 0.5 | 206 | 208 |
| Coinbase | 1.0 | 125 | 94 |
| Kraken | 1.0 | 29 | 17 |

Order signing takes 1.6 ms typical and 5 ms p99 (`order.sign`).

## 4. Orders on Polymarket (real orders from this account, Dublin)

| Key | What | n | min | p50 | p90 | p99 | max |
|---|---|---|---|---|---|---|---|
| `order.post_only.ack` | post-only sent → exchange answer (fast path) | 7,455 | 8 | 65 | 177 | 1,167 | 8,983 |
| `order.cancel.ack` | cancel sent → exchange answer (fast path) | 7,003 | 25 | 61 | 156 | 892 | 6,854 |
| `order.post_only.ack.all` | ... every live maker order (incl. 10-25's slower path) | 11,440 | 5 | 80 | 545 | 2,812 | 8,983 |
| `order.cancel.ack.all` | ... every live maker cancel | 10,758 | 25 | 67 | 321 | 1,445 | 6,854 |
| `order.fill_notice` | engine match → our fill notice (user channel) | 414 | 0.3 | 19 | 1,642 | 8,055 | 9,553 |
| `order.fak.round_trip` | FAK sent → answer, all | 509 | 152 | 614 | 1,911 | 3,233 | 5,250 |
| `order.fak.round_trip_filled` | ... orders that filled | 68 | 208 | 382 | 1,058 | 2,301 | 2,905 |
| `order.fak.round_trip_killed` | ... orders that were killed | 433 | 221 | 663 | 2,024 | 3,322 | 5,250 |
| `order.fak.queue_gap` | killed FAK: asks vanished → answer | 295 | 3 | 267 | 878 | 2,171 | 2,248 |

* **Post-only orders and cancels** come from the two live imb_maker runs. The defaults are imb-maker-10's fast
  order path (10-27). imb-maker-live on 10-25 was much slower: place p50 148 ms and p90 1.2 s, cancel p50 95 ms
  and p90 553 ms (`order.post_only.facts.by_bot`). Even on the fast path, 1 in 10 cancels took over 156 ms and 1
  in 100 over 0.9 s. Those slow cancels are when a maker gets picked off, because the same BTC move that makes
  us cancel sends takers in.
* **Fill notices** are measured on 414 of imb's 456 real fills, matched to the public print of the same trade
  (351 of them cross-token). Half arrive within 19 ms, but 10% arrive more than 1.6 s late. During that time the
  bot keeps quoting with inventory it doesn't know it has.
* **FAK orders** cover all 509 market orders ever sent: fast-value-live, spikebot-live and manual. The fill rate
  is 13.4% overall and 7.7% for the 326 orders that could be lined up with the recorded book. 98.8% of them
  still had asks at the limit when sent.

## 5. The taker hold, and why paper takers lied

For each real FAK, the recorded book shows when the asks at the order's limit disappeared. For filled orders
that moment is our own fill, so the makers' pull time is censored. Kaplan–Meier over all 326 orders gives the
chance that the stale asks are still there t ms after sending:

| t (ms) | 100 | 250 | 500 | 750 | 1,000 | 1,500 | 1,750 | 2,000 | 3,000 |
|---|---|---|---|---|---|---|---|---|---|
| asks still there | 90% | 75% | 48% | 28% | 20% | 10% | 6.3% | 5.5% | 1.3% |

The real fill rate, 7.7%, equals this curve at **H = 1,701 ms** (`order.fak.model.hold_ms`). The models we
used before, checked on the same orders (`validation.fak_models`):

| Model | Predicted fill rate | Real | Agrees per order |
|---|---|---|---|
| fixed 50 ms (old paper default) | 93.9% | 7.7% | 12.6% |
| fixed 713 ms | 29.1% | 7.7% | 71.8% |
| random draw of real round trips (stale_taker) | 36.7% | 7.7% | 64.5% |
| fast_player queue model, 2,200 ms | 5.5% | 7.7% | 91.1% |
| **calibrated hold 1,701 ms (`fak_match_ms`)** | **8.3%** | 7.7% | 89.6% |

Agreement on raw rows is biased against filled orders, because their recorded vanish is our own take. The
fill rate is the calibration target. The order's own real response time agrees 93.9%: matching against the book
as it stands when the answer comes is right.

**Market reaction after a Binance futures 1 s move** (BTC 5m, 10–285 s into the window, ask 0.05–0.95; 9,979
signals at ≥1 bps and 2,888 at ≥2 bps):

| | 100 ms | 250 ms | 500 ms | 1 s | 2 s |
|---|---|---|---|---|---|
| favoured ask unchanged (1 bps) | 81% | 53% | 30% | 18% | 16% |
| average ask change, cents (1 bps) | +0.4 | +1.5 | +3.5 | +5.2 | +6.1 |

The first 1-tick reprice comes at a median of 263 ms, and a +2 c reprice at a median of 387 ms
(`market.reprice_*`). Taken with the 1.7 s hold: by the time a taker order can match, the stale price is
almost always gone.

## 6. How it was measured, and how to re-measure

* **`scripts/latency_probe.py --site dublin --minutes 15`**, run on the server, places no orders.
  * Network tests run first, one at a time: DNS, TCP ×30, TLS ×10, then 40 keep-alive GETs after 2 warm-ups.
  * Then every feed runs at once, each in its own process: uvloop on Linux, compression off, `time.time_ns()`
    taken right after `recv()`, raw frames kept and JSON parsed after the run.
  * The clock offset is sampled every 30 s: chrony on the server (±1 µs), SNTP elsewhere (best of 5). Each
    message is corrected by the offset interpolated at its receive time.
  * Check: a feed's minimum age should equal its minimum ping / 2. Dublin is within ±1 ms (Binance 99.6 vs 101,
    Polymarket 9.1 vs 8.1, Coinbase 45.9 vs 46.9, Kraken 8.4 vs 7.3). Stored as
    `floor_check_ms`.
* **`scripts/latency_history.py`** runs on the server at nice 10, three workers, about 22 min for 88 h.
  * One pass over the recordings, the DB's `live_orders` and the imb_maker window logs.
  * Feed ages use the recorder's receive stamps, on the same chrony clock.
  * Market reaction is measured on the recorded book.
  * For FAKs, "asks vanished" is the first moment the recorded best ask on the order's side was above its limit.
  * Fill notices: our user-channel receipt minus the public print of the same trade. That print is on our token
    at our price, or on the other token at 1 − price for the cross-token fills that are most maker fills.
* **`scripts/latency_build.py`** merges the newest probe per site and the newest history file into the profile,
  and re-runs the FAK model check.

Re-measure after any change of server, region, client library or order path, and at least monthly.

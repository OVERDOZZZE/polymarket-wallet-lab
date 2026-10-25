# Reverse-engineering CrouchPredictions and machinelearned: a reference

> **This is a reference, not a plan.** Nothing here is required. It records how the data was collected and handled
> the first time, so you don't have to work it out again: endpoints, formats and the traps we found. It also lists
> options for analysis and bot building. Pick, combine, replace or ignore any of it. The goal is the **most
> accurate reconstruction of each trader's bot and the most profitable bot built from it**, reached as efficiently
> as you can.
>
> The user wants a **fresh start**: new data and new analysis. The earlier reconstructions (`market_maker`,
> `ml_maker`, `crouch_v2`) don't match the traders' fills and lose money. Don't build on their conclusions. §7 says
> briefly why they failed, so you can avoid the same mistakes.

Written 2026-10-24.

## Model switching (the user asked for this)

The user is on a Claude Pro plan and starts you on **Opus 5.5** to save usage. **Tell the user when to switch to
Fable 5.1, and when to switch back.**

| Work | Model |
|---|---|
| Project setup, download and collection scripts, building tables, data checks, routine descriptive stats | Opus 5.5 |
| **Analysis that works out the strategy:** price model, quote and cancel reconstruction from the books, take triggers, regime and rule extraction, markout and edge interpretation | **Fable 5.1** |
| **Bot building:** designing the strategy or engine, the fill simulator, validation against the traders, writing the final bot description | **Fable 5.1** |
| Mechanical work between those phases (re-runs, data fixes, formatting reports) | Opus 5.5 |

* Before each switch, stop and tell the user plainly, e.g. **"Switch to Fable 5.1 now: the next step is <X>."** Do
  the same when switching back to Opus 5.5.
* Before suggesting a switch, save your progress and findings to files in your project, so the work continues
  cleanly after the switch or after a usage-limit reset.

## 0. Two projects: where things are

| | Path | Role |
|---|---|---|
| **This study (your project)** | `C:\Users\faust\Desktop\Projects\Polymarket Trader Analysis` | New, separate project. Put all new code, data and reports here, organised however you like. |
| **Existing app (reference)** | `C:\Users\faust\Desktop\Projects\Polymarket BTC Analysis` | v2 app: trader downloader, market-data recorder, paper bots, replay, live-trading gateway. Read its `STRUCTURE.md` first: it maps every module, table and data path. |

* **Every repo path in this file** (`backend/...`, `scripts/...`, `data/...`, `STRUCTURE.md`, `reports/latency_result.txt`)
  is relative to the **existing app's folder**. The exception is Binance bulk-file paths, which are relative to
  `https://data.binance.vision`.
* Reuse the app's code however suits you: read it, copy pieces, import it (add its folder to `sys.path`), or run its
  CLI (`.venv\Scripts\python -m backend.cli ...` from its folder).
* Its `.venv` already has pandas, pyarrow, websockets and orjson. Its data is in its `data/`:
  * the SQLite DB `data/btc5m.db`;
  * the recordings `data/recordings/`;
  * the old logger export `data/import/btc5m/`;
  * the caches `data/cache/`.
  That data is from the old attempt. Use it or ignore it as you see fit.
* **Be careful writing into the existing app.** It runs the live bots on the Dublin server, which deploys from that
  code. The user's hard rules from its `CLAUDE.md` still apply everywhere:
  * never read, print or store anything from `.env.live` or the `POLY_API_*` values in `.env`;
  * **never place, arm or cancel live orders unless the user asks in that session**.
  If you do change the app, follow its `CLAUDE.md` (paths in `backend/config.py`, schema in `backend/db.py`, update
  `STRUCTURE.md`).
* Same PC, so the same limits apply here: **Windows Application Control blocks unsigned native binaries** (no
  `pyarrow.dataset`, and test-import any new native package). Node is portable in the app's `.tools/node/` (not on
  PATH).

---

## 1. The targets

| | CrouchPredictions | machinelearned |
|---|---|---|
| Wallet (profile address) | `0x21f4c1a1be34a39a7bc20985df4b7e5a39fb691d` | `0x86b14772a4da70f3e6d5eee06933ebef143efc87` |
| Seen trading | BTC Up/Down 5m and 15m (check other assets) | BTC Up/Down 5m (check others) |

Markets: Polymarket "Bitcoin Up or Down" windows. Slug `btc-updown-5m-<start_unix>` (or `-15m-`), end = start + 300
(or 900). Each window has two tokens, Up and Down. Their books mirror each other: an Up bid at p is a Down ask at
1 − p.

---

## 2. How the markets work (verified facts worth knowing)

* **Settlement** uses the **Chainlink BTC/USD 60 s TWAP**, not the tick at the boundary. Gamma's event
  `eventMetadata` holds `priceToBeat` (TWAP at the start) and `finalPrice` (TWAP at the end). One window's
  `finalPrice` is the next window's `priceToBeat`. You can rebuild it as the mean of the 1 s Chainlink ticks in
  (B − 60 s, B] (`backend/polymarket/markets.py:twap_of`). `polymarket.com/api/crypto/crypto-price` returns the spot
  tick, which is the wrong number.
* **`priceToBeat` comes late.** It is published only after the previous window resolves, sometimes minutes into the
  window, so a bot must estimate the strike itself at the open.
* **Fees and rebates.** Takers pay a fee of about `shares × rate × p × (1 − p)`. Makers receive a share of a rebate
  pool. Rebates are paid as separate `MAKER_REBATE` / `TAKER_REBATE` activity rows (about daily), not per fill.
  `REWARD` rows exist too. Read the current fee and rebate parameters from Gamma market fields; they have changed
  before.
* **Timestamps differ by source:**
  * data-api: block time in seconds, about 1 s after the match;
  * on-chain: Polygon block time, about 2 s resolution;
  * RTDS live activity: match time in ms;
  * the CLOB market websocket: ms.
  Don't measure reaction latency with second-resolution data.
* **One transaction can hold several identical fills** (same size and price against different makers). They are
  real fills, so don't de-duplicate them.
* **The CLOB market websocket sends one `last_trade_price` per taker order**, with total size and one price, even
  when the order sweeps several levels.

---

## 3. Collecting the data

Store it in your project, laid out as you like. Parquet per day per source works well. On this PC
**`pyarrow.dataset` / `filters=` is blocked** (Application Control): use `pd.read_parquet`, or row-group pruning as in
the app's `backend.util.read_parquet_where`. Test-import any new native package before relying on it.

### 3.1 Wallet activity — Polymarket data-api
`GET https://data-api.polymarket.com/activity?user=<wallet>&start=<s>&end=<s>&limit=500&offset=<n>&sortBy=TIMESTAMP&sortDirection=DESC`
* Types include TRADE, REDEEM, MERGE, SPLIT, CONVERSION, REWARD, MAKER_REBATE and TAKER_REBATE. Keep **all** of them:
  a large share of a market maker's profit can be in the rebate rows.
* Fields: `timestamp, type, side, outcome, outcomeIndex, price, size, usdcSize, slug, title, eventSlug, conditionId,
  asset (token id), transactionHash`.
* **Paging traps.** `offset` is capped (about 5000), so page backwards in time by moving `end` to the oldest
  timestamp seen. A page's oldest second may be cut off, so re-read that second in full by walking `offset` within
  it, and don't drop the duplicate rows (§2). Working implementation: `backend/traders/sources.py:fetch_activity`.
  Crouch produced about 50 k rows per day.

### 3.2 Maker or taker for each fill
* `GET https://data-api.polymarket.com/trades?user=<wallet>&takerOnly=true` (page by time) returns only the wallet's
  **taker** fills. Activity TRADE rows not in this set are **maker** fills.
* Or take the role from the on-chain data (§3.3), which is exact.

### 3.3 On-chain fills — exact roles, fees and order IDs (Polygon)
The public APIs have no order IDs. The exchange contract's `OrderFilled` events carry `orderHash`, `maker`, `taker`,
the asset ids, the filled amounts and `fee`. Grouping fills by `orderHash` **rebuilds the trader's orders**: size,
price, partial fills, and the time from first to last fill.
* Polymarket changed exchange and order versions in 2026, so **don't use remembered contract addresses**. Take a
  known `transactionHash` from activity and call `eth_getTransactionReceipt` on a Polygon RPC. The logs give the
  current exchange address(es), the event topic and the data layout. Decode one fill by hand and confirm it matches
  the activity row.
* Bulk fetch: `eth_getLogs` on that address and topic, filtered on the indexed maker topic = wallet and separately the
  taker topic = wallet. Public RPCs cap block ranges and result sizes, so shrink the chunk on errors, or ask the user
  for a free Alchemy/Infura key. Cache block number → timestamp.
* Check which address appears in the logs: the profile address may be a proxy or deposit wallet.
* Alternative: Polymarket's orderbook subgraph on Goldsky (`orderFilledEvents`). Check that it still exists and agrees
  with the RPC data before using it.

### 3.4 The whole market's tape (competitors and counterparties)
* `GET https://data-api.polymarket.com/trades?market=<conditionId>` gives every trade in a window, with the wallets.
  Paging is capped, so fall back to on-chain logs by token id if needed.
* RTDS (`wss://ws-live-data.polymarket.com`, activity topic) streams every Up/Down trade live with match-time ms and
  both wallets. `backend/recorder.py` already records it as source `act`.

### 3.5 Market metadata, settlement and coarse price history
* Gamma: `https://gamma-api.polymarket.com/events?slug=<eventSlug>` (eventMetadata, start/end, fee fields) and
  `/markets?slug=` (token ids, conditionId). The CLOB `GET https://clob.polymarket.com/markets/<conditionId>` gives
  winners.
* `GET https://clob.polymarket.com/prices-history?market=<token_id>&startTs=&endTs=&fidelity=1` gives 1-minute
  prices, the only history available for periods we didn't record.
* Collect **every** window in your period, including the ones the traders skipped. Skipping is part of a strategy.

### 3.6 Order books — only what we record
Polymarket has no historical order-book API. Books exist only for the time a recorder was running:
* `python -m backend.recorder` subscribes to the CLOB market ws for every BTC 5m/15m window, plus Coinbase, Kraken,
  Binance spot and futures, Chainlink and RTDS activity. It writes `data/recordings/<day>/<HH>.tsv.gz` with lines
  `recv_ms\tsource\tmessage`. Sources: `bk:<slug>`, `cb`, `kr`, `bn`, `bf`, `cl`, `act`. A reader is in
  `backend/bots/replay.py` (`_gz_lines`).
* It also runs on the Dublin server (AWS EC2 eu-west-1, Ubuntu; app in `~/app`; systemd units `pmtool-server`,
  `pmtool-recorder`, `pmtool-bot@<id>`). Connect with `ssh -i dublin-key-aws.pem ubuntu@108.130.59.134`, run from the
  existing app's folder, where the key file lives. The IP changes whenever the instance is stopped and started, so
  ask the user if it fails. Its recordings are in `~/app/data/recordings/` and **are kept only 7 days**, so copy them
  off regularly (e.g. `scp -r`) if you need a long micro-structure period.
* This kind of data **can't be backfilled**. If quotes, cancels and ms timing matter to your approach, start
  recording\
* Rather than relying on the existing recorder, you can also write a dedicated one (for example full depth only for
  windows the traders touch, or the CLOB user channel).
* Finding a trader's quotes: a fill of an order of known size S at price p implies an earlier +S size increase at p
  in the book (placement). A −S drop with no trade print is a cancel. Distinctive sizes make the matching reliable.

### 3.7 Binance (bulk historical, free)
`https://data.binance.vision`. Daily zips with `.CHECKSUM` files; `<D>` = YYYY-MM-DD:
* spot aggTrades: `data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-<D>.zip`
* spot trades: `data/spot/daily/trades/BTCUSDT/BTCUSDT-trades-<D>.zip`
* spot 1 s klines: `data/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-<D>.zip`
* USD-M futures aggTrades: `data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-<D>.zip`
* USD-M futures bookTicker: `data/futures/um/daily/bookTicker/BTCUSDT/...` (not published for every day; check)
* futures `bookDepth`, `metrics`, funding: optional context

**Units:** spot files moved to **microsecond** timestamps in 2025, while futures use ms. Detect the unit per file.
REST `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s` works for small gaps
(`data-api.binance.vision` is a mirror). Live websockets are in `backend/config.py`. Earlier measurements found
Binance futures to be a strong short-term lead.

### 3.8 Other price feeds
* **Coinbase** BTC-USD: REST history from `https://api.exchange.coinbase.com/products/BTC-USD/trades` (slow paging),
  live ws in config. It was the fastest feed we measured.
* **Chainlink** 1 s btc/usd: live only, via RTDS (`crypto_prices_chainlink` topic). Record it, or use Gamma
  priceToBeat/finalPrice per window.
* **Kraken**, **OKX**, **Bybit**, **Deribit** (implied vol) are possible extra signals.

### 3.9 Live capture of the traders
RTDS activity filtered by wallet gives each fill with ms match time. The app's `traders.live.LiveHub` does this and
writes to `trader_events`. A standalone collector is just as fine.

### 3.10 Measured network and order timings

Everything here was measured by us between 2026-10-20 and 2026-10-23, all in ms. Use it to reason about who can react
how fast, to set latencies in simulators, and to read the traders' reaction times. The numbers drift over time; scripts
to re-measure are listed at the end.

**Where things are**
* Polymarket CLOB (matching engine and order API): **AWS London, eu-west-2**. From a London box, TTFB was 16–18 ms
  (the location itself). The UK is close-only for the order API, so it can't be used for trading.
* Our server: **AWS Dublin, eu-west-1b** (c7i.xlarge). Ireland is geoblocked only on the frontend: the geoblock
  endpoint says blocked, but real post-only orders are accepted.
* Home PC: Winnipeg, Canada, about 130 ms from the CLOB.
* Binance is served from AWS Tokyo, far from both.

**From the Dublin server**

| What | Median | Spread / notes |
|---|---|---|
| CLOB HTTP round trip (warm HTTP/2) | 19–22 | 17–20 in a later test |
| Polymarket book feed delay (exchange stamp → received) | ~10 | |
| Post-only order: send → response | 44 | 37 in a later test |
| Post-only order: send → visible on the public book feed | ~26 | p90 ~35 |
| Cancel: send → response | 29–36 | |
| Cancel: send → gone from the public book feed | ~26 | p90 ~31 |
| **Taker / marketable (FAK) order: send → response** | **~460** | 207–2670; first live orders 276–407, one 1641 |
| Binance TCP connect (`stream.binance.com`) | ~201 | min 198 |
| Binance spot feed age (trade stamp → received) | ~105 | p10 104, p90 107 |
| Coinbase TCP connect (Cloudflare edge) | ~1 | the edge, not the matching engine |
| Coinbase feed age | ~47 | p10 46, p90 54 |
| Kraken RTT | ~28 | |
| Chainlink btc/usd via RTDS, source stamp → received | ~1330 | p10 1032, p90 1680 |
| RTDS relay of Chainlink (Polymarket send → received) | ~276 | p10 180, p90 385 |
| Local clock against NTP (time.cloudflare.com) | +0.6 | SNTP delay 1.2 ms |

**Which BTC feed moves first, as received in Dublin** (2026-10-23, 15 min, from `reports/latency_result.txt`)
* Spike race (≥ 0.5–1 bps move within 1 s): Binance spot bookTicker fires first. Binance spot trades are about
  1 ms later. **Coinbase is about 90–150 ms behind Binance** (median; IQR roughly 0–500). **Chainlink is about
  1.4–1.8 s behind.**
* Cross-correlation: Coinbase about 40 ms after Binance; Chainlink about 2 s after Binance.
* Binance comes first even though its feed takes about 105 ms to reach Dublin, so Binance leads the price itself.
  The fast-player study found Binance **futures** to be the most useful extra signal.
* Polymarket repricing: after a ≥ 2 bps Coinbase move in 1 s, the Up price completes about 38 % of its repricing in
  that second, 81 % within 1 s and 99 % within 2 s. Polymarket trade rate is 2.4–2.7× normal 0.2–0.5 s after the
  move (old logger study).

**From the home PC (Winnipeg)** (old logger, 2026-10-20, clock-corrected)
* Ping to clob.polymarket.com: about 40 ms average, 142 ms max. Warm HTTPS round trip about 135 ms.
* Post-only order placed in about 160 ms (p90 about 310). Cancel about 155 ms. FAK market orders 344–406 ms.
* Feed age: Coinbase 34 ms (p90 49), Kraken 66–74 ms (p90 86–100), Polymarket book 64–73 ms (p90 130–250, with 0.3–1.8 s
  lags in spikes), Chainlink 1.32 s (p90 1.8 s).
* A paper fill rule that matched 28 of 30 real FAK outcomes: fill against the book as it was at
  **send time + 450 ms** of Polymarket time. The live answers took 529 ms median, and the match happened about 80 ms
  before the answer.

**Signing (our own order build and sign, local CPU)**
* EIP-712 signing with `coincurve`: **about 0.5–0.8 ms** per order. Pure-Python signing is about 5 ms.
* The first order on a new token costs **about 400 ms**, because the SDK (`polymarket-client`) fetches and caches tick
  size and fee data. `backend/trading/gateway.py` avoids this by pre-signing a throwaway order (never posted) for each
  upcoming window's tokens. After that, about 1 ms.
* Other speed tricks in the gateway:
  * L2 API credentials loaded from `.env`, so no derive round trip;
  * one warm HTTP/2 connection, kept alive by a cheap authenticated call every 8 s;
  * market orders carry their worst price, so there is no book fetch;
  * order work runs on its own thread pool.
* The CLOB closes an HTTP/2 connection after 10,000 requests (GOAWAY), so reconnect at once.
* The gateway stores `sign_ms` and `post_ms` for every live order (`live_orders.data`), so live orders show where the
  time goes.

**Polymarket delays taker orders on purpose**
* **Marketable / taker orders (FAK, or limits that cross) are held** before matching. From Dublin they took about
  460 ms median (207–2670), against about 26–44 ms for post-only orders and cancels on the same connection. That
  rules out the network. It looks like a deliberate taker speed bump; check Polymarket's docs or changelog for the
  current rule and length.
* **Post-only (maker) orders and cancels are not delayed.** A maker can pull or move a quote in about 26 ms, well
  inside a taker's hold.
* What this means:
  1. Latency-arbitrage *taking* is mostly blunted. Our first 4 live FAK buys of fast-value-live all missed ("no
     orders found to match") while the paper twin filled all 4. Paper simulations that let a taker fill at the book
     as seen at signal time are far too optimistic.
  2. Fast *makers* gain: they see the move (Binance about 100 ms before Coinbase) and cancel stale quotes before
     held takers arrive. If the traders reprice quotes within tens of ms of a Binance move, they are probably hosted
     near London and use Binance.
  3. Model taker fills as `send + hold (about 450–500 ms, heavy tail)` against the book at that later time.
     Competitors' takes are held the same way.

**Coinbase limit:** about 10 BTC-USD `ticker` subscriptions per IP. After that, a subscription is refused and goes
silent; the `matches` channel carries the same trades.

**Re-measuring**
* `scripts/maker_latency_test.py` runs N post-only $0.05 orders plus cancels, timing sign, post, visible on feed,
  cancel and gone from feed. **It places real (tiny) orders**, so the user runs it or explicitly approves it.
* `scripts/geotest_order.py` checks whether a location may trade, with one post-only $0.05 order. Also real.
* Feed race and lag report (`scripts/feed_latency_test.py`, the source of `reports/latency_result.txt`): spike race and cross-correlation of Binance,
  Coinbase and Chainlink over 15 min, with TCP connect times and an SNTP clock check. It places no orders.
* Simple checks: TCP connect time to each host, and the age of each websocket message (receive time minus the
  venue's stamp) with an NTP-checked clock (`backend.util.ntp_offset_ms`).

---

## 4. Analysis options

A menu. Use whatever finds the strategy fastest.

**Accounting first.** Break daily PnL into trading (by market family) + rebates + rewards + merges. A strategy that
looks flat on trades can be very profitable on rebates, and that changes what you are reverse-engineering.

**Regimes.** These bots change settings every few days (order sizes, price ranges, activity). Run change-point
detection on daily and hourly series before fitting anything; a pooled fit can produce rules no regime ever used.
Use out-of-sample days for every reported result.

**Decisions to explain:**
* which windows they trade, and when in the window they start and stop;
* which side and what price: relative to best bid/ask, mid, and a fair value;
* order size: shares or $, banded by price, by time, by inventory;
* maker vs taker, and what triggers a take;
* requote and cancel behaviour (needs recorded books);
* inventory limits, skew, caps, and end-of-window behaviour;
* exits: sells, merges, or hold to settlement.

**Approaches:**
* Descriptive distributions and conditional tables: price, size and timing by t_in_window, distance to strike,
  volatility, inventory.
* **Fair-value fitting.** Try candidate models (for example `Φ(ln(S/K)/(σ√τ))` with different S sources, σ
  estimators, drift, a TWAP correction near the end) and keep the one that best explains their quotes, takes and
  calibration.
* **Event studies and lead-lag** at ms resolution: what happens on each feed just before their fills, quote changes
  and takes. This reveals their data source and their latency, which hints at where they are hosted.
* **Non-event sampling.** Compare moments they acted against moments they could have and didn't. This is essential
  for finding triggers.
* **Markouts and adverse selection** at 1/5/10/30/60 s and at settlement, by role and bucket. Identify the
  counterparties that cost them.
* **Order reconstruction** from on-chain `orderHash`, and **quote tracking** in the recorded books.
* **Interpretable models**: trees, logistic models, piecewise rules. Use black-box models (gradient boosting +
  SHAP) only to find features.
* **Behaviour cloning / inverse RL**: learn a policy straight from (state → action) pairs.
* **The two traders against each other**: overlap, interaction, shared or opposite flow.
* **Market context**: their share of volume and of top-of-book time, and who else quotes.

**Checks worth making:** sums against activity totals, TWAP against finalPrice, mirrored-book side mapping,
timestamp source per row, and the clock offset of whatever machine recorded the data.

---

## 5. Building bots from the findings — options

**You don't have to build the production bot yourself.** When you reach this step, your deliverable can be an
**exact build description** for each bot. The user will build it in the existing app
(`C:\Users\faust\Desktop\Projects\Polymarket BTC Analysis`) and deploy it to the Dublin VPS straight away. Make the
description complete enough to implement without guessing:
* data feeds and how each one is used;
* the state the bot keeps;
* every decision rule, with exact parameters (per regime if there are regimes);
* order types, prices, sizes and timing;
* cancel and requote logic;
* risk limits;
* settlement and accounting;
* the latency assumptions it depends on;
* how to check the built bot against your results: expected per-window behaviour and test cases. Reference code or
  a prototype simulator from your project is welcome.

**You are not tied to the existing engine or code structure.** If the old `PaperBot` engine, the fill simulator or
the app's bot structure limit what the strategy needs, design a new engine, architecture or concept for building
bots, and describe it. The user will adapt the app to it. If the existing engine or structure fits well, use it.
Either choice is fine; pick what makes the bot most accurate and most profitable.

* **Rule-based reconstruction** of each trader, as a spec with parameters per regime.
* **Learned policy** (behaviour cloning) running on live features.
* **Improved variant**: keep their edge, then remove what loses. Markout analysis shows the toxic fills; filter them.
* **Live copy / follow** of their fills through the RTDS stream. Earlier tests: following machinelearned's fills
  tracked it well (PnL correlation about 0.95, about 70 % of its ROI). Simulating the strategy instead did not.
* **Hybrids**, or a strategy of your own that exploits what you learn about them, such as their predictable quotes.

What the existing app offers (optional; paths are in its folder, §0):
* `backend/bots/strategies/market_maker.py:PaperBot`: feeds, books, paper fills, settlement and persistence. Subclass
  it as `ml_maker.py` does.
* `backend/bots/replay.py`: offline replay of recordings through bot configs.
* `backend/bots/registry.py`: bot registration.
* `backend/trading/gateway.py`: the only place for live orders.

You may also write your own engine or simulator if that serves the goal better (see the start of this section).

**Validating a reconstruction:** compare window by window against the trader's real fills: traded or not, fill count,
$ and VWAP per side, timing, size histogram, maker/taker split, per-window PnL. Correlations near 1 mean the copy is
right; pooled averages can hide a wrong copy.

**Fill simulation is where earlier work went wrong.** A paper maker bot looks profitable when the simulator is too
generous. Model:
* queue position;
* cancels ahead of and behind you;
* real latency;
* the ordering of book deltas against trade prints. About 22 % of prints arrived after the matching book change.

Real makers here showed about −2c markout at 10 s. A simulated bot showing positive markout is a warning sign. One
useful test: run the trader's own reconstructed orders through your simulator and check they fill when they really did.

**Going live:** you build and paper-test. Only the user arms live trading. Never place, arm or cancel live orders
unless the user asks in that session.

---

## 6. Practical notes
* Windows 11 with PowerShell plus Git Bash. The existing app's `.venv` and `.tools/node` can be reused, or make your
  own venv in your project.
* Large data: Crouch had about 2.4 M activity rows over 50 days. One Binance spot aggTrades day is several million
  rows. The recorder writes about 4 GB/day gzipped.
* Make fetches resumable and idempotent (skip finished days, write atomically).
* If you add modules, tables, routes or data paths to the existing app, update its `STRUCTURE.md`.

## 7. Why the first attempt failed (context only)
* Strategy rules were fitted from fills alone, with no order IDs and no quote or cancel data, and pooled across size
  regimes.
* The fill simulator was optimistic: it double-counted queue progress, treated cancels behind the bot as ahead of it,
  and used 80 ms latency against 150–300 ms real.
* The bots' fair value didn't match the traders'. Per-window PnL correlation with the traders was about 0.
* Rebates and 15m windows were left out of the first profit picture.

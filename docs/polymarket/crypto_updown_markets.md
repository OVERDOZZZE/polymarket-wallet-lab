# Crypto Up/Down markets

Read 2026-10-27. Tags as in trading_rules.md. "Live" = Gamma/CLOB responses for real markets fetched that day.

## 1. Variants and what each settles on

| Series (Gamma `series_slug`) | Slug example | Resolution source | Rule | Source |
|---|---|---|---|---|
| `btc-up-or-down-5m` | `btc-updown-5m-<start_unix>` | `data.chain.link/streams/btc-usd-twap-60s-streams`, `cryptoMarketConfig btc-5m-twap-60` | Up if end TWAP **≥** start TWAP | live |
| `btc-up-or-down-15m` | `btc-updown-15m-<start_unix>` | same stream, `btc-15m-twap-60` | same | live |
| `btc-up-or-down-4h` | `btc-updown-4h-<start_unix>` | same stream, `btc-4h-twap-60` | same | live |
| `btc-up-or-down-hourly` | `bitcoin-up-or-down-october-27-2026-7pm-et` | **Binance BTC/USDT 1h candle** | Up if close ≥ open | live |
| `btc-up-or-down-daily` | `bitcoin-up-or-down-on-october-28-2026` | **Binance BTC/USDT 1m candle close at 12:00 ET** vs next day's | Up if later close higher, Down if lower, **exactly equal → 50-50** | live |

* TWAP markets: "The market resolves Up when the final price is **equal to or greater** than the price to beat"
  [official] concepts/resolution. **Ties go Up.**
* Market text: "this market is about the price according to the TWAP Chainlink data stream for the asset pair BTC/USD,
  not according to any other sources or spot markets" [live].
* TWAP assets: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE (+ ZEC on the TWAP feed) [official] PolyBolt symbols;
  Aug 2026 reward table lists BTC, SOL, ETH, HYPE, XRP, BNB, DOGE for 5m/15m/4h [official].
* Forex Up/Down markets resolve on Pyth (`usdchf` etc.) [official] changelog 2026-10-21.
* Gamma still returns an old **open** pre-TWAP 5m market (`btc-updown-5m-1766162100`, 2025-12-19, spot `btc-usd`
  stream) for `closed=false` queries: filter by date, never trust "first open market" [ours].

## 2. TWAP history

| Date | Change | Source |
|---|---|---|
| until 2026-08-06 | single Chainlink `btc-usd` print at start/end | [official] changelog 2026-08-07 |
| 2026-08-07 | TWAP resolution: 5m = 30 s lookback; 15m and 4h = 60 s | [official] |
| 2026-08-14 00:00 UTC | 5m moved to **60 s** | [official] |

## 3. Getting the TWAP (this is a change for us)

* **PolyBolt** `wss://ws-live-v2.polymarket.com/ws`, channel **`price.crypto.twap`**, filter
  `{"symbol":"btcusd","window_seconds":60}` streams "Chainlink-computed crypto TWAPs with a fixed 60-second window".
  Needs CLOB API credentials (`op: auth`). Each subscribe sends a 2-minute history snapshot. `windowSeconds` is the
  lookback, not the update rate; "Only 60 carries data today" [official] api-reference/live-data/overview, wss/polybolt.
  Example payload: `{"symbol":"btcusd","timestamp":1788886177000,"value":"78803.715261094101516288","windowSeconds":60}`.
* Live since 2026-09-15; Python SDK `CryptoTwapPriceSpec` from polymarket-client 0.11.0 [official/GitHub].
* RTDS also had a TWAP topic, `crypto_prices_twap_sixty` (and `..._thirty`, which has no replacement and does not
  match any settlement) [official] migrate/rtds-to-polybolt. **Our memory note said "the TWAP stream is not on RTDS":
  that was wrong.**
* Settlement values: Gamma **event** `eventMetadata.priceToBeat` / `finalPrice` (our note); empty on a future
  window [ours]. `/v2/resolutions` gives resolution lifecycle and `expected_settlement_time` [official] changelog 2026-09-28.

Our reconstruction (mean of 1 s Chainlink ticks observed in [end−62 s, end−3 s]) is exact to the cent in ~49% of
windows, median error $0.02 [ours, updown-settlement-rule]. Reading `price.crypto.twap` should give the value
Chainlink itself publishes. Still to verify: that the TWAP point stamped at the boundary equals `finalPrice` exactly,
and when that point reaches Dublin (see opportunities.md O3).

## 4. Window schedule and timing

* Markets are created ~24 h ahead and accept orders from creation (BTC 5m and 15m for 23:15Z were created 2026-10-26
  23:22Z) [ours]. Gamma `startDate` = creation, `eventStartTime` = window start, `endDate` = window end.
* The CLOB's `end_date_iso` for a 5m market is the **date** only (`2026-10-27T00:00:00Z`): use the slug or Gamma
  `endDate` for timing [ours].
* Trading stops at resolution; "Trading stops — You can no longer buy or sell tokens" [official] concepts/resolution.
  The docs do not state whether the book stays open between window end and resolution. Our data showed trading in
  the seconds around the end; treat post-end fills as possible until `market_resolved` / `closed`.
* `market_resolved` event on the market stream with `custom_feature_enabled` [official].
* Resting orders left on a resolved market end `CANCELED_MARKET_RESOLVED` [GitHub enum].

## 5. Fees and programs on these markets

* Taker fee 0.07·p(1−p) on 5m (since launch 2026-02-12), 15m (since 2026-01-05), and all crypto incl. 1h/4h/daily/weekly
  created after 2026-03-06 [official] changelog. Maker rebate 20%, per-market pools.
* Taker delay **150 ms** on crypto Up/Down (`itode: true`) [official]; also on finance Up/Down markets [announcement].
* `rfqEnabled: false`, `comboStatus: disabled`, `negRisk: false`, `version: v1` (CTF, not Protocol V2),
  `restricted: true` [ours]. `restricted` is undocumented (likely "subject to geo restrictions").

## 6. Market facts from the docs that a 5m bot should respect

* Tick 0.01, min 5 shares [ours].
* Cancels during a delayed taker's window fail; our own taker orders cannot be pulled for 150 ms.
* After a matching-engine restart, the first 2 min are post-only [official]. A restart near a window's end blocks
  late takers.
* `/v2/resolutions` answers are cached up to 5 s while live [official] changelog 2026-10-07.

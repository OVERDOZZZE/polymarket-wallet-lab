# Timeline of rule changes, 2025-2026

Dates are UTC. [official] = docs.polymarket.com/changelog/predictions unless another page is named.

| Date | Change | Source |
|---|---|---|
| 2025-05-15 | CLOB limits raised: POST/DELETE /order 500/10 s burst, 3,000/10 min | [official] |
| 2025-05-28 | FAK order type; market WS: no 100-token cap, `initial_dump` | [official] |
| 2025-06-03 | Batch orders (max 5); `side` on MakerOrder in /data/trades | [official] |
| 2025-07-23 | `min_order_size`, `neg_risk`, `tick_size` in /book | [official] |
| 2025-08-21 | Batch orders max 15 | [official] |
| 2025-08-26 | Data API /trades, /activity: limit ≤ 500, offset ≤ 1,000 | [official] |
| 2025-09-15 | `price_change` WS message restructured | [official] |
| 2025-09-24 | RTDS released (Binance + Chainlink prices, comments) | [official] |
| ~2025-11 | 500 ms taker delay ("speed bump") on crypto markets | [third-party] Yahoo/GuruFocus 2026-03-16, cointelegraph |
| 2026-01-05 | Taker fees on 15m crypto; daily maker rebates | [official]; The Block 2026-01-06 |
| 2026-01-06 | Heartbeats API; post-only orders | [official] |
| 2026-01-16 | Maker rebates: fee-curve weighting documented | [official] |
| 2026-02-11 | Rebates computed **per market**; sports fees from 02-18 | [official] |
| 2026-02-12 | **5-minute crypto markets** launched, with taker fees | [official] |
| ~2026-02-18 | 500 ms taker delay removed without notice | [third-party] X posts, Protos 2026-02-20 |
| ~2026-02-25 | 250 ms taker delay on 5m, 15m, hourly crypto | [third-party] Yahoo/GuruFocus 2026-03-16 |
| 2026-03-06 | Fees + rebates on all new crypto markets (1h, 4h, daily, weekly) | [official] 2026-03-01 |
| 2026-03-23 | ToS + Market Integrity Rules (spoofing, wash trading, disruptive practices) | [third-party] BusinessWire; [official] integrity.polymarket.com |
| 2026-03-30/31 | Fee Structure V2 (categories); use `feeSchedule` | [official] |
| 2026-04-08 | POST /order 5,000/10 s burst, 48,000/10 min | [official] |
| 2026-04-09/10 | Gamma `closed` defaults false; keyset pagination | [official] |
| 2026-04-21 | Relayer /submit returns without hash | [official] |
| 2026-04-28 | **CLOB V2** live: new exchange contracts, pUSD, V2 order struct, fees at match time, open orders wiped | [official] |
| 2026-05-04 | All new account wallets are Deposit Wallets | [official] wallets-auth |
| ~2026-05-13 | Co-location in eu-west-2 for KYC/KYB users added to docs | [third-party] bitcoinfoundation.org; [official] geoblock page |
| 2026-05-14 | /markets/keyset max limit 100 | [official] |
| 2026-05-27 | Report: faster trading offered for ID verification; verification portal | [third-party] The Information via PYMNTS/BeInCrypto |
| 2026-05-28 | **Taker Rebate Program** and new Referral terms | [official] programs |
| 2026-06-01 | POST/DELETE /order 120,000/10 min sustained; batch 2,000/10 s | [official] |
| 2026-06-05 | Taker orders **locked** (non-cancellable) during the 250 ms delay on crypto and finance Up/Down | [announcement] @PolymarketDevs 2026-06-03 |
| 2026-06-15 | DELETE /orders max 1,000 IDs | [official] |
| 2026-06-29 | New Deposit Wallets are beacon proxies | [official] wallets-auth |
| ~2026-07-06 | Alberta, BC, Quebec added as restricted (with Ontario) | [third-party] overdog.bet |
| 2026-07-10 | Sports fee 0.05, sports rebate 15% | [official] |
| 2026-07-14/17 | Old Neg Risk Adapter retired for relayer | [official] |
| 2026-07-24 | Async commit pipeline: FAK/FOK return tradeIDs only; per-signer rate limiter in warning mode | [official] changelog 07-17, trading-rate-limits |
| ~2026-08-06 | Per-signer limits enforced (unconfirmed) | [third-party] dev.to |
| 2026-08-07 | Crypto Up/Down resolve on Chainlink TWAP (5m 30 s; 15m, 4h 60 s); $1M August TWAP rewards | [official] |
| 2026-08-10 | Per-outcome REDEEM rows; position fee basis fields | [official] |
| 2026-08-14 | 5m TWAP window 60 s; help center geo page updated (Ireland "completely restricted") | [official] |
| 2026-08-17 11:00 | Taker delay 250 → **50 ms** | [official]; [announcement] @PolymarketDevs |
| 2026-09-04 14:00 | Taker delay 50 → **150 ms**; Data API v2 live, v1 frozen | [official] |
| 2026-09-15 | **PolyBolt** WS (`price.crypto`, `price.crypto.twap`, `price.equity`); RTDS prices legacy | [official] |
| 2026-09-23 | polymarket-client 0.11.0 (PolyBolt); RTDS price topics to be removed one month later | [GitHub] PyPI; [official] migration guide |
| 2026-09-28/29 | `/v2/resolutions` settlement estimates; PolyBolt `source` field, forex TWAPs | [official] |
| 2026-10-02 | `price.crypto` defaults to Chainlink for all symbols; provider pins | [official] |
| 2026-10-07 | `/v2/tokens`; data API fixes | [official] |
| 2026-10-21 | polymarket-client 0.14.0: heartbeats, rebates read, cancel-reason statuses; PolyBolt market_status, forex | [GitHub]; [official] |
| ~2026-10-23 | Planned removal of RTDS price topics (still served on 10-27) | [official] migration guide |
| 2026-10-24 | **Data API v1 retired** (still served on 10-27) | [official] docs banner |
| ~2026-10-26 | Our account: every resting order cancelled after ~16 s; card deposits ask for ID + selfie | [ours] |

SDK releases (polymarket-client, PyPI upload dates): 0.1.0 2026-07-22, 0.2.0 07-24, 0.3.0 08-04, 0.4.0 08-06,
0.5.0 08-07, 0.6.0 08-13, 0.7.0 08-26, 0.8.0 09-03, 0.9.0 09-04, 0.10.0 09-10, 0.11.0 09-23, 0.12.0 10-01,
0.13.0 10-09, **0.14.0 10-21** (we run 0.14.0). A release PR for 1.0.0 exists in Polymarket/py-sdk (#359, closed) [GitHub].

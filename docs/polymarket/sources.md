# Sources

All read on **2026-10-27** unless noted. Raw copies of the docs pages were fetched as markdown (`<page>.md`) from the
index at https://docs.polymarket.com/llms.txt (90 pages + 30 API-reference pages).

## Official: docs.polymarket.com

| Page | URL |
|---|---|
| Docs index | https://docs.polymarket.com/llms.txt |
| Predictions changelog | https://docs.polymarket.com/changelog/predictions |
| SDK changelog | https://docs.polymarket.com/changelog/sdks |
| Geographic restrictions + co-location | https://docs.polymarket.com/api-reference/geoblock |
| Rate limits (Cloudflare) | https://docs.polymarket.com/api-reference/rate-limits |
| Trading rate limits (per signer) | https://docs.polymarket.com/api-reference/trading-rate-limits |
| Get rate limit tier | https://docs.polymarket.com/api-reference/account/get-rate-limit-tier |
| Send heartbeat | https://docs.polymarket.com/api-reference/trade/send-heartbeat |
| Post a new order / multiple orders | https://docs.polymarket.com/api-reference/trade/post-a-new-order , .../post-multiple-orders |
| Cancel multiple orders | https://docs.polymarket.com/api-reference/trade/cancel-multiple-orders |
| Market channel / user channel WS | https://docs.polymarket.com/api-reference/wss/market , .../wss/user |
| PolyBolt overview / channel | https://docs.polymarket.com/api-reference/live-data/overview , .../wss/polybolt |
| Order lifecycle | https://docs.polymarket.com/concepts/order-lifecycle |
| Resolution | https://docs.polymarket.com/concepts/resolution |
| Positions & tokens (holding rewards) | https://docs.polymarket.com/concepts/positions-tokens |
| pUSD | https://docs.polymarket.com/concepts/pusd |
| Place orders | https://docs.polymarket.com/trading/place-orders |
| Manage orders (heartbeats, closed-only) | https://docs.polymarket.com/trading/manage-orders |
| Real-time order updates | https://docs.polymarket.com/trading/realtime-order-updates |
| Fees | https://docs.polymarket.com/trading/fees |
| Market making | https://docs.polymarket.com/trading/market-making |
| Matching engine restarts | https://docs.polymarket.com/trading/matching-engine |
| Wallets and authentication | https://docs.polymarket.com/trading/wallets-auth |
| Session keys | https://docs.polymarket.com/trading/session-keys |
| Manage positions | https://docs.polymarket.com/trading/positions/manage |
| Combos overview / market makers | https://docs.polymarket.com/trading/combos/overview , .../combos/market-makers |
| Market details | https://docs.polymarket.com/market-data/market-details |
| Real-time data | https://docs.polymarket.com/market-data/realtime-data |
| Taker Rebate Program | https://docs.polymarket.com/programs/taker-rebates |
| Maker Rebates Program | https://docs.polymarket.com/programs/maker-rebates |
| Liquidity Rewards | https://docs.polymarket.com/programs/liquidity-rewards |
| Referral Program | https://docs.polymarket.com/programs/referral-program |
| Builder tiers | https://docs.polymarket.com/programs/builders/tiers |
| Migrate RTDS → PolyBolt | https://docs.polymarket.com/migrate/rtds-to-polybolt |
| Migrate Data API v1 → v2 | https://docs.polymarket.com/migrate/data-api-v1-to-v2 |
| Protocol V2 migration | https://docs.polymarket.com/migrate/polymarket-v2/overview |
| Contracts | https://docs.polymarket.com/resources/contracts |
| On-chain position data | https://docs.polymarket.com/resources/onchain-position-data |
| Error codes | https://docs.polymarket.com/resources/error-codes |
| Perps self-trade prevention | https://docs.polymarket.com/perps/learn-about-trading/self-trade-prevention |
| AsyncAPI (market/user WS schema) | https://docs.polymarket.com/asyncapi.json |
| KYC/KYB co-location form (linked from geoblock page) | https://docs.google.com/forms/d/e/1FAIpQLSfY-3Dl3yxq8HKFjFad8YzKZmm0k3Gdg29HD6gL-K-AmI6KXw/viewform |

## Official: other Polymarket pages

| Page | URL |
|---|---|
| Help: Geographic Restrictions (updated 2026-08-14) | https://help.polymarket.com/en/articles/13364163-geographic-restrictions |
| Help: Limit Orders (sports delays) | https://help.polymarket.com/en/articles/13364444-limit-orders |
| Help: Maker Rebates / Trading Fees / Liquidity Rewards | https://help.polymarket.com/en/articles/13364471-maker-rebates-program , .../13364478-trading-fees , .../13364466-liquidity-rewards |
| Help: Exchange upgrade 2026-04-28 | https://help.polymarket.com/en/articles/14762452-polymarket-exchange-upgrade-april-28-2026 |
| Help: Contact support | https://help.polymarket.com/en/articles/13364256-how-do-i-contact-support |
| Transparency (last modified 2026-08-18) | https://polymarket.com/transparency |
| Market Integrity Rules | https://integrity.polymarket.com |
| Terms of Use (JS-rendered; body not retrievable) | https://polymarket.com/tos |

## Live API responses [ours]

| What | URL |
|---|---|
| Gamma market (BTC 5m, 15m) | https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-1793142900 , .../btc-updown-15m-1793142900 |
| CLOB market info (`itode`) | https://clob.polymarket.com/clob-markets/0x76fd63b3a137ed0a62bc5f8cfad723ce37d005ab05e5cb28bbd876bb460b4aa2 |
| CLOB market | https://clob.polymarket.com/markets/0x76fd63b3a137ed0a62bc5f8cfad723ce37d005ab05e5cb28bbd876bb460b4aa2 |
| Series variants (5m/15m/hourly/4h/daily) | https://gamma-api.polymarket.com/events?series_slug=btc-up-or-down-hourly&closed=false&limit=1 (and siblings) |
| Data API v1 still serving | https://data-api.polymarket.com/trades?limit=1 |
| Market WS level 1/2/3 capture | wss://ws-subscriptions-clob.polymarket.com/ws/market |
| PyPI release dates | https://pypi.org/pypi/polymarket-client/json |

## GitHub

| Item | URL |
|---|---|
| Installed SDK polymarket-client 0.14.0 (`models/clob/user_events.py`, `order_response.py`, `notifications.py`) | .venv/Lib/site-packages/polymarket |
| ts-sdk PR #445: INVALID and CANCELED_* statuses | https://github.com/Polymarket/ts-sdk/pull/445 |
| ts-sdk #364: open orders empty for POLY_1271 | https://github.com/Polymarket/ts-sdk/issues/364 |
| rs-clob-client-v2 #122: WS 1013 slow consumer | https://github.com/Polymarket/rs-clob-client-v2/issues/122 |
| py-clob-client-v2 #34: ghost fills | https://github.com/Polymarket/py-clob-client-v2/issues/34 |
| py-clob-client-v2 #111: maker address not allowed | https://github.com/Polymarket/py-clob-client-v2/issues/111 |
| py-sdk #221: rate-limit state surfaced | https://github.com/Polymarket/py-sdk/pull/221 |
| py-sdk #359: release 1.0.0 PR | https://github.com/Polymarket/py-sdk/pull/359 |

## Announcements (Polymarket staff)

| Item | URL |
|---|---|
| @PolymarketDevs: Jun 5 taker-order lock during 250 ms delay | https://x.com/PolymarketDevs/status/2061930696323830158 |
| @PolymarketDevs: 250 → 50 ms on Aug 17 | https://x.com/PolymarketDevs/status/2089295325660172578 |
| Same Jun 5 notice, full text via Jinse/Bitget (2026-06-03) | https://www.bitget.com/news/detail/12560605442364 |

## Third-party (unverified)

| Item | URL |
|---|---|
| Co-location for KYC'd users (2026-05-13) | https://bitcoinfoundation.org/news/prediction-markets/polymarket-api-opens-fastest-trading-access/ |
| The Information report via PYMNTS (2026-05-27) | https://www.pymnts.com/authentication/digital-identity/2026/polymarket-debuts-new-id-verification-measures-for-customers/ |
| BeInCrypto via Yahoo: VPN policing, AML-triggered verification | https://finance.yahoo.com/markets/crypto/articles/polymarket-blocking-vpn-access-kyc-181022107.html |
| GNcrypto: KYC after ISP blocks | https://www.gncrypto.news/news/polymarket-tightens-kyc-after-isp-blocks-geoblocking/ |
| Bitcoin.com: KYC push | https://news.bitcoin.com/report-polymarket-pushes-kyc-on-crypto-traders-as-geoblocking-gaps-draw-global-scrutiny/ |
| Value The Markets: VP says KYC beta-only | https://www.valuethemarkets.com/cryptocurrency/news/polymarket-clarifies-no-identity-verification-for-main-platform-amid-beta-testing |
| Protos: 500 ms delay removed (2026-02-20) | https://protos.com/polymarket-ends-trading-loophole-for-bitcoin-quants/ |
| Yahoo/GuruFocus: 250 ms replacement on 5m/15m/hourly (2026-03-16) | https://finance.yahoo.com/news/polymarkets-5-minute-bitcoin-bets-123329673.html |
| X: 500 ms removal posts (2026-02-18) | https://x.com/Jesterthegoose/status/2024055036754686034 |
| The Block: 15m fees (2026-01-06) | https://www.theblock.co/post/384461/polymarket-adds-taker-fees-to-15-minute-crypto-markets-to-fund-liquidity-rebates |
| TradoxVPS: TWAP settlement and taker delay summary | https://tradoxvps.com/polymarket-twap-settlement/ |
| dev.to: rate-limit enforcement ~Aug 6 | https://dev.to/abrownfox001/polymarket-clob-rate-limits-api-updates-august-2026-what-bot-developers-must-know-1pfo |
| Integrity rules coverage (DeFi Rate) | https://defirate.com/news/polymarket-publishes-enhanced-market-integrity-rules-for-us-defi-platforms/ |
| Canada provinces | https://overdog.bet/polymarket-canada , https://sigma.world/news/polymarket-faces-wider-restrictions-across-canada/ |
| Glassnode HyperLatency (method) | https://latency.glassnode.com/prediction-markets/about |
| NYCServers latency guide (2026-04-07) | https://newyorkcityservers.com/blog/polymarket-server-location-latency-guide |

Not reachable: Telegram t.me/s/polytradingapis (no public preview), BusinessWire release (403), polymarket.com/tos
body (client-rendered).

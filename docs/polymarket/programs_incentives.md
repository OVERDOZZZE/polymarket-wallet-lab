# Programs and other products

Read 2026-10-27. Fees, maker/taker rebates, liquidity and holding rewards, referrals: see fees_rebates_rewards.md.

## 1. Builder Program

[official] programs/builders/overview, tiers, fees:
* For apps that route **other users'** orders. Attribution by a `builderCode` (bytes32) on each order (native since
  V2; copy it from polymarket.com/settings?tab=builder). Builders may charge builder taker fees on top of platform fees.
* Tiers: Unverified (100 relayer tx/day, standard API limits), Verified (10,000/day, weekly USDC rewards on volume,
  grants, Telegram; apply at builder@polymarket.com), Partner (unlimited, "Highest" API rate limits, priority access).
* **Co-location is not available to Builder Program participants** [official] api-reference/geoblock.
* For us: no value while we trade only our own account. A Verified builder gets 10,000 gasless relayer
  transactions/day, which matters only if we merge/redeem a lot through the relayer.

## 2. Combos and RFQ (`fetch_rfq_status` in the SDK)

* RFQ is the quote system for **Combos**: multi-leg YES/NO positions built from Combo-enabled markets. A requester
  asks; connected market makers have **400 ms** to quote; the requester has **10 s** to accept; optional **last look 1 s**
  [official] trading/combos/overview.
* Market makers connect over `asyncapi-rfq` websocket, authenticate as the order signer, approve ExchangeV3 /
  PositionManager / Router [official] trading/combos/market-makers.
* Combos pay a 0.05 taker fee rate and **0% maker rebate** [official] trading/fees.
* BTC Up/Down: `rfqEnabled: false`, `comboStatus: disabled` [ours] → **not usable on our markets** today.
* "Collateral Return" lets compatible Combo positions release pUSD before resolution [official].

## 3. Perps

Polymarket Perps is a separate exchange (own API, margin, funding, liquidation, tiered maker/taker fees, builder
codes, liquidity rewards, referral). It has documented **self-trade prevention**, which the prediction CLOB lacks
[official] perps/*. Not part of our Up/Down strategy; a BTC perp could in principle hedge Up/Down inventory, but that
needs its own study.

## 4. Polymarket US

CFTC-regulated DCM run by QCX LLC; full KYC; for US users; separate API (docs.polymarket.us) and SDKs
(polymarket-us-python / -typescript) [official]/[third-party]. Not open to a Canadian resident.

## 5. Market-making support

"For market maker onboarding and support, contact support@polymarket.com" [official] trading/market-making. There is no
published designated-market-maker programme with fee or priority benefits; incentives are maker rebates and
liquidity rewards. Rate-limit tiers can be manually assigned [official] trading-rate-limits.

## 6. Announcement channels

* Telegram `t.me/polytradingapis` (Trading APIs; no public web preview), Discord #trading-api-announcements, status page
  status.polymarket.com, X `@PolymarketDevs` [official] trading/matching-engine, changelog.
* Docs changelogs: /changelog/predictions, /changelog/sdks, /changelog/perps [official].

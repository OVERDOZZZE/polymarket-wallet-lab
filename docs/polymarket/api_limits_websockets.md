# API limits and websockets

Read 2026-10-27. Tags as in trading_rules.md.

## 1. Two independent limiters

1. **Cloudflare, per IP** — requests over the limit are **throttled (delayed/queued), not rejected**; sliding windows
   [official] api-reference/rate-limits.
2. **Per-signer token buckets** for order and cancel requests — over the limit returns **429** [official]
   api-reference/trading-rate-limits. Warning mode from 2026-07-24 for two weeks (`Poly-RateLimit-Warning: true`);
   "Polymarket will announce when live enforcement begins". I found no official enforcement-start notice; one
   third-party says ~2026-08-06 [third-party] dev.to.

## 2. Cloudflare limits (current page)

| Endpoint | Burst | Sustained |
|---|---|---|
| `POST /order` | 5,000 / 10 s | 120,000 / 10 min (200/s) |
| `DELETE /order` | 5,000 / 10 s | 120,000 / 10 min |
| `POST /orders` | 2,000 / 10 s | 21,000 / 10 min |
| `DELETE /orders` | 2,000 / 10 s | 15,000 / 10 min |
| `DELETE /cancel-all` | 250 / 10 s | 6,000 / 10 min |
| `DELETE /cancel-market-orders` | 1,500 / 10 s | 21,000 / 10 min |
| CLOB general | 9,000 / 10 s | |
| `/book` 1,500, `/books` 500, `/price` 1,500, `/midpoint` 1,500 per 10 s; balance-allowance GET 200, UPDATE 50 | | |
| Ledger `/trades`, `/orders`, `/order` 900; `/data/orders`, `/data/trades` 500; `/notifications` 125 per 10 s | | |
| API-key endpoints 100 / 10 s; Gamma general 4,000 (`/markets` 300, `/events` 500); Data v2 general 800 (`/v2/trades` 300, `/v2/activity` 200); Relayer `/submit` 25 / min | | |

The 2026-06-01 changelog quoted `POST /orders` and `DELETE /orders` at "2000 every 10s (200/s) (BURST)"; the sustained
numbers in the table above are the current page.

## 3. Per-signer token buckets

* One **order** bucket and one **cancel** bucket per signer (the address of the API credentials). Costs: POST /order 1;
  POST /orders = number of orders; DELETE /order 1; DELETE /orders = number of IDs; cancel-all and cancel-market = 1
  + number actually cancelled (can push the bucket negative on lower tiers).
* **Batches are all-or-nothing**: a batch is admitted only if the bucket has tokens for every entry; a batch costing
  more than the burst can never pass.
* Tier from the **maker wallet's 30-day volume**, refreshed every 3 h:

| Tier | 30-day volume | Orders/s | Order burst | Cancels/s | Cancel burst | Negative cancel balance |
|---|---|---|---|---|---|---|
| **Standard** | — | **40** | **60** | **80** | **120** | yes |
| Copper | $30k | 60 | 90 | 120 | 180 | yes |
| Bronze | $50k | 80 | 120 | 160 | 240 | yes |
| Silver | $100k | 200 | 300 | 400 | 600 | yes |
| Gold | $500k | 400 | 600 | 800 | 1,200 | yes |
| Platinum | $2.5M | 450 | 675 | 900 | 1,350 | no |
| Diamond | $5M | 525 | 787 | 1,050 | 1,575 | no |
| Elite | $10M | 600 | 900 | 1,200 | 1,800 | no |

* Read the tier: `GET /v1/user/rate-limit-tier` (L2 auth) → `tier`, `usdc_volume`, `next_tier_threshold`, `tier_details`.
  Headers on every covered response: `Poly-RateLimit-Remaining`, `Poly-RateLimit-Reset`, `Poly-RateLimit-Tier`;
  on 429 `Retry-After` [official].
* "Contact support@polymarket.com if ... you believe your wallet qualifies for a higher tier, recently moved trading
  activity to a new address" [official].
* SDK: polymarket-client surfaces rate-limit state (`on_rate_limit_update`) since 2026-07-31 [GitHub] py-sdk #221.

Our exposure: ~6k orders/hour is 1.7/s on average, well inside Standard. But imb-maker's re-quote of 13 levels × 2
tokens as one batch costs 26 tokens; three re-quotes inside ~1 s empty the 60-token burst and the next batch is
rejected whole.

## 4. Market channel (`wss://ws-subscriptions-clob.polymarket.com/ws/market`)

* Subscribe: `{"assets_ids": [...], "type": "market", "initial_dump": true, "level": 2, "custom_feature_enabled": true}`.
  No subscription cap since 2025-05-28. Add/remove assets live with `{"operation": "subscribe"|"unsubscribe", ...}`.
* Events: `book` (snapshot + `hash`), `price_change` (array of level updates with `best_bid`/`best_ask`, structure
  changed 2025-09-15), `last_trade_price` (with `transaction_hash`, `fee_rate_bps`), `tick_size_change`; with
  `custom_feature_enabled`: `best_bid_ask`, `new_market`, `market_resolved` [official].
* Text `PING` every 10 s → `PONG`. `level` 1/2/3 undocumented (see latency_infrastructure.md §5).

## 5. User channel (`.../ws/user`)

* First frame `{"auth": {apiKey, secret, passphrase}, "type": "user", "markets": [...]}`; send at once or the server
  may close. Market filters can be changed live [official].
* `event_type: order` with `type` PLACEMENT / UPDATE / CANCELLATION and `status`; `event_type: trade` with
  MATCHED/MINED/CONFIRMED/RETRYING/FAILED, `maker_orders[]`, `trader_side` [official].
* After reconnect, re-read open orders and trades: the stream does not replay [official].

## 6. PolyBolt (`wss://ws-live-v2.polymarket.com/ws`) — replaces RTDS for prices

* Channels: `price.crypto` (default **Chainlink**, `provider: pyth` pin possible for BTC/ETH/SOL/XRP/DOGE/BNB),
  `price.crypto.twap` (Chainlink, 60 s), `price.equity`, `price.equity.twap`. All need CLOB API credentials
  (`{"op":"auth",...}` → `authed`) [official].
* RTDS price topics (`crypto_prices` Binance, `crypto_prices_chainlink`, `crypto_prices_twap_sixty`) are deprecated and
  "planned for removal one month after the 0.11.0 release" [official] migrate/rtds-to-polybolt. 0.11.0 shipped
  2026-09-23 [GitHub/PyPI] → planned removal ~2026-10-23. RTDS still served us after that date; it can stop any day.
  The RTDS comments stream is being retired with no replacement.
* Limits and close codes: latency_infrastructure.md §5.

## 7. Data API v1 → v2

* "**Data API v1 is retired on October 24, 2026.**" [official] docs banner + migrate/data-api-v1-to-v2. v1 `/trades`
  and `/activity` still answered HTTP 200 on 2026-10-27 [ours].
* Mapping: `/activity` → `/v2/activity`, `/trades` → `/v2/trades`, `/positions` and `/closed-positions` →
  `/v2/positions?status=...`. v2 wraps payloads in `data`, uses opaque cursors (no offset), snake_case, and returns 429
  with `Retry-After` under load [official].
* v1 limits for `/activity` and `/trades` were already capped at limit 500 / offset 1,000 (2025-08-26) [official].
* New: `/v2/user-pnl`, `/v2/user-stats`, `/v2/user-volume`, `/v2/resolutions`, `/v2/tokens`, `/v2/prices-history`
  (moved from CLOB) [official].
* We call v1 (`config.DATA_API`) in backend/traders/sources.py:116 (`/activity`, the trader download),
  backend/traders/live.py:548-571 (`/activity`, `/positions`, `/value`), backend/trading/gateway.py:439 (`/positions`)
  and scripts/latency_probe.py:50 (`/trades`) [ours].

## 8. Gamma

* `GET /markets` and `/events` default `closed=false` since 2026-04-09; keyset variants `/markets/keyset`,
  `/events/keyset` (max limit 100) replace offset paging [official] changelog.

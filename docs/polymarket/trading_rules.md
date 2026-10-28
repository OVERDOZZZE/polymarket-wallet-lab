# Trading rules: orders, matching, cancels, restarts

Read 2026-10-27. Tags: [official] docs.polymarket.com, [GitHub] Polymarket repos / installed SDK, [announcement]
Polymarket staff posts, [third-party, unverified] everything else. Our own measurements are marked [ours].

## 1. Order types

| Type | Behaviour | Source |
|---|---|---|
| GTC | rests until filled or cancelled | [official] trading/place-orders |
| GTD | rests until `expiration`. **Expires 1 minute before the stated time**; expiration must be **≥ 3 minutes** in the future, so the shortest effective life is ~2 min. For N seconds of life use `now + 60 + N` | [official] trading/place-orders |
| FOK | fill all now or nothing | [official] |
| FAK | fill what is available now, cancel the rest. Error when nothing matches: `no orders found to match with FAK order...` | [official], SDK `fak_not_filled` |
| post-only | flag on GTC/GTD only. Rejected with `invalid post-only order: order crosses book` if it would match | [official] api-reference/trade/post-a-new-order |

* "Market orders" are limit orders priced to cross. Market orders must use `expiration: "0"` [official].
* A market BUY `amount` is the **pre-fee** USD notional; fees are charged on top [official] place-orders "Cap Market Buy Spending".
* `orderType`, `postOnly` and `deferExec` (boolean, "Whether to defer execution", default false, otherwise undocumented)
  sit in the POST body, outside the signed order [official] clob-openapi `SendOrder`.
* V2 order struct (since 2026-04-28): `nonce`, `feeRateBps`, `taker` removed; `timestamp` (ms), `metadata`, `builder` added.
  Fees are set at match time [official] changelog 2026-04-17.

## 2. Order statuses

POST response `status`: `live` (resting), `matched`, `delayed` (marketable, inside a matching delay), `unmatched`
(marketable, placed on the book after the delay without a match) [official] concepts/order-lifecycle.

User-channel order statuses known to SDK 0.14.0 [GitHub] (`polymarket/models/clob/user_events.py`):
`LIVE, MATCHED, DELAYED, UNMATCHED, CANCELED, CANCELED_MARKET_RESOLVED, CANCELED_NOT_ENOUGH_BALANCE,
CANCELED_MARKET_NOT_FOUND, CANCELED_EXPIRED_ORDER, CANCELED_ORDER_TIMED_OUT, INVALID`.
ts-sdk PR #445 (2026-10-21) describes `INVALID` as "system-removed orders" and `CANCELED_*` as cancellations with a
reason (e.g. market resolved, FOK kill) [GitHub]. **A plain `CANCELED` is therefore the ordinary cancel path, not one of
the reasoned system removals.** This matters for our ~16 s cancels (see account_kyc_restrictions.md §5).

Trade statuses: `MATCHED → MINED → CONFIRMED` (terminal), `RETRYING`, `FAILED` (terminal) [official].
Since V2, `MATCHED` can come long before on-chain settlement or never settle ("ghost fills", 1-50+ s; 9 of ~50 trades
on btc-updown-5m on 2026-04-28) [GitHub] py-clob-client-v2 issue #34 (open). Do not size a sell from `size_matched`.

## 3. Taker delay (crypto Up/Down)

| Fact | Source |
|---|---|
| Current value **150 ms** since 2026-09-04 14:00 UTC (was 50 ms from 2026-08-17 11:00 UTC, 250 ms before) | [official] changelog |
| "The order is held ..., then validation runs again and the order is matched or placed on the book. **The API waits for this hold and returns the final order result.**" (page still says 250 ms: stale) | [official] concepts/order-lifecycle |
| "During either delay, the order is pending and **cannot be canceled**. If the market, balance, allowance, or risk checks fail when the delay expires, the order is rejected instead of matching." | [official] order-lifecycle |
| Lock started 2026-06-05: cancel attempts rejected with "cannot cancel because the order is in pending/delayed status"; funds reserved during the window; resubmitting the same order in the window is rejected; disconnecting does not stop it | [announcement] @PolymarketDevs 2026-06-03 via Bitget/Jinse |
| Which markets: `GET https://clob.polymarket.com/clob-markets/{condition_id}` → `itode: true`. Live BTC 5m on 2026-10-27: `"itode":true` | [official] + [ours] |
| `secondsDelay` / `seconds_delay` (Gamma/CLOB) is the separate *sports* delay; it is 0/None on BTC 5m | [official] market-details + [ours] |
| Sports: 3 s delay on marketable orders; 1 s taker delay tested on NBA/MLB | [official] help.polymarket.com "Limit Orders" |

No document explains our measured ~1.7 s effective FAK hold (latency-profile memory). The docs only say the hold is
followed by re-validation and that the response waits for it. See open_questions.md Q3.

## 4. Matching

* Price-time priority is implied ("price improvement always benefits the taker") [official] order-lifecycle; there is no
  published queue/priority rule beyond that, and no priority for verified users.
* Maker vs taker: the order that rests is the maker [official].
* **No self-trade prevention is documented for the prediction CLOB** (STP exists only for Perps,
  perps/learn-about-trading/self-trade-prevention) [official]. Two of our bots on the same wallet can trade with each other;
  self-matching is listed as abuse in the Taker Rebate and integrity rules [official] programs/taker-rebates.
* `order {id} is invalid. Duplicated.` — identical order hash rejected [official] error-codes.
* `order canceled in the CTF exchange contract` — an order hash cancelled on-chain [official] error-codes.
* `order timed out` (500): "typically happens during bursts of concurrent order submissions from the same account. The
  order was rejected before reaching the order book and can be safely resubmitted." [official] error-codes.
* `the price of the taker's order has a discrepancy greater than allowed with the worst maker order` (500) [official].

## 5. Tick size, minimum size, precision

* Tick sizes: 0.1, 0.01, 0.005, 0.0025 (World Cup only), 0.001, 0.0001. BTC 5m/15m: **0.01**, min size **5 shares**
  (`orderMinSize` 5, `mos` 5, `mts` 0.01) [official] + [ours, live market 2026-10-27].
* Precision for tick 0.01: price 2 dp, size 2 dp, amount 4 dp; amounts round up to +4 dp then down [official] place-orders.
* `tick_size_change` event on the market stream; an order at a stale tick is rejected [official].
* Max order size = balance − Σ(open order size − filled) [official] order-lifecycle.

## 6. Batches and cancels

| Item | Limit | Source |
|---|---|---|
| `POST /orders` | 1-15 orders; each evaluated independently | [official] api-reference/trade/post-multiple-orders |
| `DELETE /orders` | **1,000** IDs (changelog 2026-06-15, API ref). The manage-orders guide still says 3,000: stale | [official] |
| `DELETE /cancel-market-orders` | by `asset_id` or `market` (condition) | [official] |
| `DELETE /cancel-all` | every open order of the API key's account | [official] |
| Partial fills | only the unfilled remainder can be cancelled | [official] |
| Cancel results | `canceled[]` + `not_canceled{id: reason}`; always reconcile | [official] manage-orders |

## 7. Order heartbeats (dead-man switch)

[official] trading/manage-orders "Order Heartbeats", api-reference/trade/send-heartbeat:
* Scope: **per API key**, all markets and processes. The first accepted heartbeat arms it. **No disarm.**
* Send every 5 s. Modern route `POST /v1/heartbeats` with a rotating `heartbeat_id` (start with `""`); a mismatch
  returns 400 `Invalid Heartbeat ID` with the expected ID. Legacy `POST /heartbeats` shares the registration and
  **resets the expected ID to ""** — mixing the two from different processes makes IDs race.
* "After 10 seconds without an accepted heartbeat, the server detects expiry on its **five-second scan** and queues a
  cancel-all attempt." So cancels land ~10-15 s (plus queue) after the last good send. Best effort.
* "The server does not keep canceling later orders indefinitely"; a later accepted send registers again.
* WebSocket PING does **not** refresh the deadline.
* SDK: `send_order_heartbeat()` added in polymarket-client 0.14.0 (2026-10-21) [GitHub].

## 8. Matching engine restarts and restricted modes

[official] trading/matching-engine:
* Announced ~2 days ahead on Telegram `t.me/polytradingapis` and Discord #trading-api-announcements.
* While restarting, order requests return **HTTP 425 Too Early** (`Retry-After`). SDK: `RequestRejectedError.restriction == "restarting"`.
* After a restart: **2 minutes of post-only mode**. Non-post-only orders get 503 `code: post_only_mode`,
  `retry_after_seconds`. In a batch, each rejected order carries the error but `success: true`.
* Cancel-only and fully disabled both return 503 `trading is disabled` on POST /order; cancels may or may not work.
* Global 503 `Trading is currently disabled. Check polymarket.com for updates` = no orders and no cancels [official] error-codes.

## 9. Market state and lifecycle

* Trade-ready = `active && !closed && acceptingOrders` [official] market-details.
* BTC 5m/15m markets are created and accept orders **~24 h before the window**: window 2026-10-27 23:15Z had
  `createdAt` 2026-10-26 23:22:30Z, `acceptingOrdersTimestamp` 23:22:45Z [ours, Gamma].
* `clearBookOnStart: false` on BTC 5m (sports books are cleared at game start) [ours] + [official] help "Limit Orders".
* Market stream `custom_feature_enabled: true` adds `best_bid_ask`, `new_market`, `market_resolved` events [official].
* After resolution trading stops; resting orders on a resolved market end as `CANCELED_MARKET_RESOLVED` [GitHub enum].

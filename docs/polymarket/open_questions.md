# Open questions and what to ask Polymarket support

Support: website chat (blue icon) or support@polymarket.com [official] help "How do I contact support".
Market makers: support@polymarket.com [official] trading/market-making. Developers: Discord #devs.
Do not paste API keys, secrets or the private key into any message. The wallet address and order IDs are fine.

## Q1. Why is every resting order on our account cancelled after ~16 s? (highest priority)

Known: status `CANCELED` with no reason; first order of a run lives 4-13 s, then each re-post 16.2-16.6 s; website
orders too; closed-only False; started ~2026-10-26; same day card deposits began asking for ID + selfie. Bots run on
AWS eu-west-1 (Dublin) since 2026-10-22; the user lives in Manitoba, Canada.

Message to send:
> Since about 2026-10-26 every resting limit order on my account (wallet 0x…, Deposit Wallet) is cancelled
> 5-16 seconds after it rests, both orders placed through the CLOB API and orders placed by hand on polymarket.com.
> The user-channel event is CANCELLATION / CANCELED with no reason. My closed-only status is false and orders are
> accepted. Example order IDs: … (with timestamps). (1) Is there a restriction, compliance review or verification
> requirement on my account that cancels open orders? (2) My trading bots run on a server in AWS eu-west-1 (Ireland);
> I live in Manitoba, Canada. Your developer docs list Ireland as frontend-only restricted and eu-west-1 as the closest
> non-georestricted region, but the help center lists Ireland as completely restricted. Is API trading from an Irish
> server allowed for a Canadian resident? (3) If I complete identity verification, will the cancellations stop?

## Q2. KYC/KYB and co-location in eu-west-2

> I trade my own account through the API and would like the co-location access described on
> docs.polymarket.com/api-reference/geoblock. I filled in / will fill in the KYC/KYB form. (1) Which documents and
> how long does approval take? (2) Is there a cost? (3) What exactly does access give: permission to trade from AWS
> eu-west-2 (London) IPs, a private network path to the CLOB that bypasses Cloudflare, or both? (4) What order/cancel
> latency should we expect from eu-west-2? (5) Is a resident of Manitoba, Canada eligible? (6) Does verification
> change anything else: taker delay, rate-limit tier, fees, rebates, queue priority?

## Q3. The 150 ms taker delay vs the ~1.7 s effective hold we measure

Our FAK orders on BTC 5m return after ~0.3-2.4 s (p50 0.9 s) and match only against asks that are still there at
response time (7.7% fill rate). The order-lifecycle page says the order is held, re-validated, then matched, and
that the API waits for the hold.

> On crypto Up/Down markets (itode=true), is the taker hold exactly 150 ms from receipt, or does the order also wait
> in a queue (per market, per account, or behind the async commit pipeline)? What is the expected time from receipt
> to match for a FAK on btc-updown-5m? Is the delay the same for every account?

## Q4. Per-signer rate limits

> Is live enforcement of the per-signer order/cancel token buckets on? (The page still describes the warning phase.)
> Does a cancel-heavy pattern (thousands of post-only orders and cancels per hour) affect account standing in any way
> beyond 429 responses?

## Q5. Heartbeats across API keys

> Do order heartbeats armed on one API key ever cancel orders placed with another key of the same account
> (for example the key the website uses)? Does polymarket.com itself use order heartbeats?

## Q6. Smaller unknowns (docs, not support)

| Question | Where to look next |
|---|---|
| Meaning of market WS `level` 1/2/3 | ask on Discord #devs |
| Meaning of `deferExec` on POST /order | ask on Discord #devs |
| `moas: 30` in `/clob-markets` rewards block; `restricted: true` on Gamma markets | Discord |
| Timing of AutoRedeemer redemptions | watch AUTO_REDEEMED notifications on our wallet |
| Whether trading continues between window end and `market_resolved` | our recordings |
| Exact relation of the `price.crypto.twap` point at the boundary to Gamma `finalPrice` | our recordings once subscribed |
| Date per-signer enforcement went live (third-party says ~08-06) | changelog / Telegram |
| Whether RTDS price topics and Data API v1 will be switched off on a fixed date | changelog |
| Exact dates of the Nov 2025 500 ms delay and the Feb 2026 250 ms replacement (third-party only) | Polymarket Discord history |

## Contradictions found in Polymarket's own pages

| Topic | Page A | Page B |
|---|---|---|
| Ireland | geoblock docs: frontend-only, API not restricted | help center (2026-08-14): completely restricted |
| Taker delay | order-lifecycle: 250 ms | changelog: 150 ms since 2026-09-04 |
| DELETE /orders max | changelog/API ref: 1,000 | manage-orders guide: 3,000 |
| Sports maker rebate | docs: 15% | help center: 20% |
| Rebate currency | docs: pUSD | help center: USDC |

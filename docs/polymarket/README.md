# Polymarket rules, mechanics and programs (research 2026-10-27)

Everything we found about how Polymarket works that can matter to our BTC Up/Down bots. Each fact carries a tag:
**[official]** docs.polymarket.com / help.polymarket.com / polymarket.com, **[GitHub]** Polymarket repos or the installed
SDK, **[announcement]** Polymarket staff posts, **[third-party, unverified]**, **[ours]** our own measurement.

| File | Contents |
|---|---|
| [trading_rules.md](trading_rules.md) | order types, GTD buffer, statuses and cancel reasons, taker delay, matching, ticks, batches, heartbeats, restarts |
| [fees_rebates_rewards.md](fees_rebates_rewards.md) | taker fee, maker rebate formula, Taker Rebate tiers, liquidity and holding rewards, referrals |
| [crypto_updown_markets.md](crypto_updown_markets.md) | 5m/15m/4h/hourly/daily variants, settlement sources, ties, TWAP stream, schedule |
| [latency_infrastructure.md](latency_infrastructure.md) | where the CLOB runs, co-location, Cloudflare, WS behaviour |
| [account_kyc_restrictions.md](account_kyc_restrictions.md) | geography (Ireland conflict, Canada), KYC, account states, integrity rules, **our 16 s cancel problem** |
| [api_limits_websockets.md](api_limits_websockets.md) | Cloudflare limits, per-signer token buckets, market/user/PolyBolt WS, Data API v2 |
| [settlement_ctf.md](settlement_ctf.md) | pUSD, contracts, split/merge/redeem, auto-redeem, wallets, session keys, Protocol V2 |
| [programs_incentives.md](programs_incentives.md) | builder program, Combos/RFQ, Perps, Polymarket US, announcement channels |
| [changelog_timeline.md](changelog_timeline.md) | dated rule changes 2025-2026 |
| [opportunities.md](opportunities.md) | every finding that could help, with value and the change we would make |
| [open_questions.md](open_questions.md) | what is unconfirmed, and messages ready for Polymarket support |
| [sources.md](sources.md) | every URL read |

## Top findings, ranked by impact on us

1. **The 16 s cancels look like an account flag, and our server location is the likely trigger.** The help center
   (updated 2026-08-14) lists **Ireland as "completely restricted"**, says "permissions are based on physical
   location rather than residency", and bans VPN-style circumvention (ToS §2.1.4). The developer docs instead call
   Ireland frontend-only. Our Dublin IP gets `blocked: true` from the geoblock endpoint. The bots moved there 10-22;
   the cancels and the ID prompt began ~10-26. Our cancels are plain `CANCELED`, not one of the reasoned system
   statuses, and the 4-13 s first life followed by fixed 16.2-16.6 s lives fits a periodic sweep of the whole
   account. Unconfirmed: ask support (open_questions Q1). [official] + [ours]
2. **Co-location in London is official**: "Users who complete the KYC/KYB form can get access to co-locate directly
   in eu-west-2"; open to own-account traders and market makers, not builders. Manitoba is not a restricted province.
   Cost, timing and latency are unpublished. [official] api-reference/geoblock
3. **Chainlink's own 60 s BTC TWAP is streamed** on PolyBolt `price.crypto.twap` (since 2026-09-15, needs CLOB creds).
   We reconstruct it from ticks and are exact in only ~49% of windows. [official]
4. **Feeds we depend on are past their retirement dates**: RTDS price topics (planned removal ~2026-10-23) and Data
   API v1 ("retired on October 24, 2026"). Both still answered on 10-27. [official]
5. **Per-signer rate limits**: Standard tier 40 orders/s (burst 60), 80 cancels/s (burst 120), tier by 30-day maker
   volume, batches all-or-nothing, 429 when over. Check `GET /v1/user/rate-limit-tier`. [official]
6. **Taker delay**: 150 ms since 2026-09-04; the order is held, **re-validated**, then matched; it cannot be cancelled
   and the response waits for the hold. `itode: true` on `/clob-markets/{id}` flags the market. Nothing official
   explains our ~1.7 s effective hold. [official]
7. **Ties go Up** on TWAP markets (`≥`). Hourly BTC Up/Down settles on the **Binance BTC/USDT 1h candle**, daily on
   Binance 1m candles with ties 50-50. [official] + live market text
8. **Heartbeats are per API key with no disarm**: 10 s expiry, checked on a 5 s scan, cancel-all best effort;
   legacy and modern routes share one registration. [official]
9. **Maker rebates are per market and weighted by p(1−p)**: crypto pool = 20% of that market's taker fees; a 0.99
   fill is worth ~1/25 of a 0.50 fill, so late-fav earns almost no rebate. **TAKER_REBATE = Taker Rebate Program**
   (since 2026-05-28): crypto weight 2.3, 3% at $2k weighted volume up to 50%. [official]
10. **Matching-engine restarts** return 425, then 2 minutes of post-only; announced ~2 days ahead on Telegram
    (`t.me/polytradingapis`) and Discord. [official]

## Our seven beliefs, checked

| # | Belief | Verdict | Notes and source |
|---|---|---|---|
| 1 | Up/Down settle on Chainlink btc-usd 60 s TWAP; fit = mean of ticks in [end−62, end−3] | **Confirmed** (TWAP); the fit is ours | 60 s for 5m since 2026-08-14, for 15m/4h since 08-07; start and end from the same stream; **ties resolve Up**. Chainlink's TWAP itself is on PolyBolt. RTDS had `crypto_prices_twap_sixty` too, so "not on RTDS" was wrong. [official] |
| 2 | Taker-only fee 0.07·p(1−p); makers get 20% as daily MAKER_REBATE; some takers get TAKER_REBATE | **Confirmed** | Rebate is per market, fee-curve weighted, $1 minimum, pUSD. TAKER_REBATE = tiered Taker Rebate Program by 30-day weighted volume. [official] |
| 3 | 500 ms removed Feb 18; 250 ms from Jun 5; 50 ms Aug 17; 150 ms Sep 4; locked during delay | **Partly contradicted** | 250 ms started ~2026-02-25 [third-party]; **Jun 5 only added the lock** [announcement]; 50 ms and 150 ms [official]. 500 ms dates are third-party only. No documented reason for a ~1.7 s hold; the docs mention re-validation after the hold. |
| 4 | Rate limits (Jun 1): POST/DELETE /order 200/s sustained; /orders 2,000 per 10 s | **Confirmed but incomplete** | Current page: sustained 120,000/10 min for /order; /orders 21,000/10 min, DELETE /orders 15,000/10 min. **Per-signer buckets** (Standard 40 orders/s) also apply and are stricter. [official] |
| 5 | Without a heartbeat for 10 s, all open orders of those credentials are cancelled | **Confirmed with detail** | Per API key, after the first accepted heartbeat only; 5 s scan; best effort; no disarm; does not keep cancelling later orders. [official] |
| 6 | UK close-only for the API; Ireland frontend-only; KYC/KYB users may co-locate in eu-west-2 | **Confirmed in the developer docs, contradicted for Ireland by the help center** | Help center: Ireland "completely restricted"; location = physical location. Canada: BC, ON, AB, QC close-only incl. API. [official] |
| 7 | ID verification (May 2026) with faster trading for verified users | **Confirmed (third-party), official only as co-location** | The Information 2026-05-27; a Polymarket VP said KYC was for a beta product. No official sign that verification changes delay, limits, fees or priority. |

## The two open problems

* **16 s cancels:** analysis and read-only checks in [account_kyc_restrictions.md §5](account_kyc_restrictions.md);
  support message in [open_questions.md Q1](open_questions.md).
* **KYC and co-location:** the only documented step is the KYC/KYB Google Form on the geoblock page; questions for
  support in [open_questions.md Q2](open_questions.md).

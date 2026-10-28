# Account rules: geography, KYC, restrictions, enforcement

Read 2026-10-27. Tags as in trading_rules.md.

## 1. Geography

Two official lists, and **they disagree about Ireland**.

| Jurisdiction | Developer docs (api-reference/geoblock) | Help center "Geographic Restrictions" (updated 2026-08-14) |
|---|---|---|
| UK (GB) | close-only on frontend **and API** | "completely restricted" |
| **Ireland (IE)** | close-only on **frontend only; "the API itself is not restricted"** | listed among "39 countries ... **completely restricted from accessing Polymarket**" |
| Japan, Netherlands, South Korea, Malta (sports) | frontend only | JP, NL, MT completely restricted; KR not listed |
| Canada: **BC, Ontario, Alberta, Quebec** | close-only frontend + API | blocked regions |
| Canada: Manitoba and other provinces | not listed | not listed |
| US, FR, DE, IT, AU, BE, PL, SG, TW, TH, RU, ... | close-only frontend + API | completely restricted (PL/SG/TH/TW "close-only") |
| OFAC (IR, SY, CU, KP, Crimea/Donetsk/Luhansk) | blocked completely | blocked |

Other rules from the help center [official]:
* "Polymarket **strictly prohibits the use of VPNs or similar tools** to bypass geographic restrictions. Such actions are
  considered violations of the platform's Terms of Service (**Section 2.1.4**)."
* "**Permissions are based on physical location rather than residency**; for instance, users traveling to
  non-restricted regions, such as Bulgaria, can continue to trade."
* Transparency page: users "of tools meant to evade geographic restrictions" are barred; "Circumvention is a violation
  of the Terms of Use, handled through the same enforcement process" (wallet-level restriction) [official] polymarket.com/transparency.

Our case [ours, dublin-server memory]: `GET polymarket.com/api/geoblock` from the Dublin server returns
`blocked: true` (country IE). API orders from Dublin are accepted. The user lives in Manitoba, which is not
restricted. Under the help-center wording, a bot in Ireland trades from a "completely restricted" location. Under
the developer docs it is allowed. **This is the strongest documented candidate for an account flag** (§5).
Alberta, BC and Quebec were added around a July 2026 terms update [third-party] (overdog.bet, sigma.world).

## 2. KYC / identity verification

* International platform: sign-up by email, Google or wallet; no ID needed by default [official] help "How to Sign Up";
  "Polymarket requests additional information from top-volume traders" [official] transparency.
* May 2026: Polymarket launched a portal for passports, licences and proof of address and offered **faster trading**
  (co-location) to verified users (The Information, 2026-05-27) [third-party]. A Polymarket engineering VP said
  KYC applied only to a beta product (2026-05-28) [third-party, conflicting].
* "Traders with large positions or rapid deposit-trade-withdraw cycles have been documented triggering verification
  under internal anti-money laundering thresholds" [third-party] BeInCrypto/Yahoo 2026-05-27.
* Accounts flagged for VPN use or geoblock evasion "can be suspended or restricted" [third-party] gncrypto.news.
* **Official benefit of verification: co-location in eu-west-2** (latency_infrastructure.md §2). Nothing official says
  verification changes the taker delay, rate-limit tier, fees or rebates. Rate-limit tiers follow 30-day volume and
  can be "manually assigned" (support can change a tier) [official] trading-rate-limits.
* Polymarket US (CFTC-regulated, QCX LLC) requires full KYC; US residents only; separate API at docs.polymarket.us
  [official]/[third-party]. Not available to us.

How to get KYC/KYB + co-location (what is documented): fill in the KYC/KYB Google Form linked from
api-reference/geoblock. Nothing more is published (no cost, timing, documents list). Ask support (open_questions.md Q2).

## 3. Account-level restriction states the API exposes

| State | How it shows | Source |
|---|---|---|
| Closed-only mode | `GET /auth/ban-status/closed-only` → `{"closed_only": bool}`; orders fail `'{address}' address in closed only mode` | [official] manage-orders, error-codes |
| Banned address | orders fail `'{address}' address banned` | [official] error-codes |
| Geoblocked IP | "Orders submitted from blocked regions will be rejected" | [official] geoblock |
| Exchange modes | 425 restarting, 503 post-only / cancel-only / disabled | [official] matching-engine |
| Rate-limit tier | `GET /v1/user/rate-limit-tier`, `Poly-RateLimit-Tier` header | [official] |

Our account: closed-only = False, orders accepted, so not banned and not closed-only [ours].

## 4. Market-integrity rules (ToS update 2026-03-23)

Prohibited on the international platform: spoofing, wash trading, fictitious transactions, self-dealing,
front-running, information misuse, attempted manipulation and "**disruptive practices** that undermine the orderly
operation of our markets". Enforcement: "We can ban wallets, take legal action, or refer matters to law enforcement"
[official] integrity.polymarket.com; [third-party] BusinessWire/DeFi Rate 2026-03-20/23. No document defines
"disruptive practices" or sets an order-to-trade or cancel-rate limit. No rule bans automated trading; Polymarket's
own docs describe bots, heartbeats and market making.

April 2026: wallets tied to "ghost transactions" (exploiting the off-chain match / on-chain settle race) were banned
[third-party].

## 5. Our open problem: every resting order cancelled ~5-16 s after resting (since ~2026-10-26)

What we observed [ours, order-heartbeat-cancels memory]: user-channel `CANCELLATION`, status **`CANCELED`**, no
reason; first order of a window lives 4-13 s, each re-post then lives 16.2-16.6 s; website orders on the same
account die too; other users' orders at the same price live; closed-only False; heartbeats every 5 s changed
nothing; card deposits started asking for ID + selfie at the same time.

What the documentation adds:
1. **Status.** System removals carry a reason (`CANCELED_MARKET_RESOLVED`, `_NOT_ENOUGH_BALANCE`,
   `_MARKET_NOT_FOUND`, `_EXPIRED_ORDER`, `_ORDER_TIMED_OUT`) or `INVALID` [GitHub SDK enum, ts-sdk #445]. Ours are
   plain `CANCELED`, the ordinary cancel path (user cancel, cancel-all, cancel-market, heartbeat cancel-all,
   session-key revocation). So *some* cancel request is being issued against the account. Note that the heartbeat
   cancel is also a cancel-all, so it may also show plain `CANCELED`.
2. **Timing.** "First order 4-13 s, then 16.2-16.6 s every time" is the signature of a **periodic sweep of ~16.4 s
   that cancels everything open**, with the bot re-posting right after each sweep. One-off events (revocation,
   single heartbeat expiry) would not repeat. The heartbeat expiry runs on a 5 s scan after 10 s of silence, so a
   sender that arms the switch and then sends only every ~15 s, or sends heartbeats that are rejected, would produce
   10-15 s periods. Our accepted 5 s heartbeats should rule that out for *our* key, but not for another key on the
   same account (heartbeats are per API key; `GET /auth/api-keys` lists all keys of the signer).
3. **Account-wide.** A per-key mechanism (heartbeat) would hit website orders only if the website uses the same key or
   arms its own heartbeat. A compliance flag would explain account-wide cancels, the new ID check, and the date
   (bots moved to Dublin on 2026-10-22; Ireland is "completely restricted" in the help center; geoblock says
   `blocked: true` for the server IP; "permissions are based on physical location").

Ranked hypotheses (none confirmed):
* H1 compliance/geo flag: account active from a blocked-location IP (Dublin datacenter) → periodic open-order sweep
  until verification. Evidence: timing coincidence with the Dublin move, simultaneous ID prompt, help-center rule.
* H2 a second heartbeat sender on another API key of the account (e.g. a website session or old process) that arms
  the switch and lapses → periodic cancel-all. Check `GET /auth/api-keys` and whether any old process still runs.
* H3 an internal anti-spam/AML rule after ~6k orders/hour bursts. No documented rule; rate limits return 429, they
  do not cancel resting orders.

Read-only checks before contacting support (no orders needed):
* `GET /auth/api-keys` (L1/L2 auth): how many keys exist on the signer.
* `GET /notifications`: whether the cancels produced `ORDER_CANCELLATION` (type 1) notifications. The payload has
  no reason field, and the docs do not say which cancels create one; compare with an order we cancel ourselves.
* `GET /data/order/{id}` for one cancelled order: status string as REST reports it.
* `GET /v1/user/rate-limit-tier`: is the tier `standard` or manually assigned (no `next_tier_threshold`).
* The `owner` / `order_owner` field on our CANCELLATION events: our key, or another one.
The next test that needs an order (user must run it): one tiny resting order placed **from Canada** with the bots
stopped, to see whether it also dies at ~16 s (the 10-27 website test suggests yes, which would point to an account
flag rather than the order's IP).

## 6. Wallet types and keys (account standing aspects)

* Deposit Wallet (signature type 3, `POLY_1271`): default for every account wallet deployed on/after 2026-05-04;
  Proxy (Magic/Google) and Safe (MetaMask) are legacy [official] trading/wallets-auth.
* Deposit Wallets after 2026-06-29 are ERC-1967 beacon proxies: Polymarket can upgrade their code; owner can opt out
  (pause → timelock → optOut, needs POL gas) [official].
* **Session keys**: the owner can authorise a separate signer for up to **180 days** with scopes `CLOB`, `COMBOSRFQ`;
  "Revoking a Session Key ... cancels its open orders without affecting orders placed by other Session Keys"
  [official] trading/session-keys. Lets the server hold a scoped key instead of the owner key.
* Open bug: `listOpenOrders` / `fetchOrder` return empty/null for POLY_1271 deposit-wallet accounts while orders are
  live (ts-sdk #364, open since 2026-09-22) [GitHub]. Cancel by ID works. Relevant when we reconcile open orders.

# Opportunities for our bots

Ordered by expected impact. Value is a rough estimate; "change" is what we would do. Nothing here was implemented.

| # | Finding | Value | Change |
|---|---|---|---|
| O1 | **Fix the account flag (16 s cancels)** — the docs point at a compliance/geo cause (Dublin IP `blocked: true`, help center: Ireland "completely restricted", "permissions are based on physical location") | Critical: no live maker works until resting orders survive | Run the read-only checks in account_kyc_restrictions.md §5; complete ID verification; ask support (open_questions.md Q1). Decide the bot location with support's answer |
| O2 | **KYC/KYB → co-location in London** (eu-west-2), own-account traders eligible, Manitoba not restricted | High: sanctioned location + ~6-20 ms per request (more if the path skips Cloudflare); cancel races for makers | Fill in the KYC/KYB form; ask for the access method, cost and latency; re-measure with latency_probe.py from London before moving bots |
| O3 | **Exact settlement TWAP on PolyBolt** `price.crypto.twap` (btcusd, 60 s) | High for late-fav and anything near the strike: today our reconstruction is exact in ~49% of windows, so the "never trust < $3" margin may shrink | Subscribe from the recorder and the bots (needs CLOB creds); log its value at each boundary vs `finalPrice`; measure arrival time vs RTDS ticks; then tighten late-fav's lock gate |
| O4 | **RTDS price topics are past their planned removal date** (~10-23) | Protects every bot's Chainlink feed | Move Chainlink spot to PolyBolt `price.crypto` (default Chainlink) with RTDS as fallback; config.RTDS_WS stays for comments only |
| O5 | **Data API v1 retired 10-24** (still answering) | Protects trader downloads, live trader watch, gateway positions | Port `/activity`, `/positions`, `/value`, `/trades` calls to `/v2/*` (cursor paging, `data` wrapper) |
| O6 | **Per-signer token buckets**: Standard 40 orders/s, burst 60; batches all-or-nothing | Avoid silent batch rejections in fast re-quoting (imb/guard makers) | Read `Poly-RateLimit-Remaining` from responses, pace batches, call `/v1/user/rate-limit-tier`; split ladders so one re-quote ≤ burst |
| O7 | **Ties resolve Up** (5m/15m/4h TWAP `≥`); hourly = Binance 1h candle `≥`; daily = Binance 1m candle, tie 50-50 | Small but free: when computed TWAP ≈ strike, Up is favoured by the tie rule | Make `fetch_winner`/sims use `≥` for Up; keep separate settlement logic if we ever trade hourly/daily |
| O8 | **Taker delay mechanics**: 150 ms hold, then re-validation; cancel impossible; funds reserved; same order resubmitted in the window rejected | Correct sims; avoid double-spend assumptions | Model the 150 ms hold explicitly inside fak_match_ms; never resend an identical FAK within the window; reserve funds for in-flight FAKs |
| O9 | **Matching-engine restarts**: 425 + 2-min post-only mode, announced on Telegram/Discord | Avoid error storms and missed windows | Handle 425 / 503 `post_only_mode` / `trading is disabled` in the Gateway; join the Telegram channel |
| O10 | **Cancel-reason statuses** (`CANCELED_*`, `INVALID`) | Diagnosis | Log the full `status` string of every CANCELLATION in live_orders (we may be collapsing them) |
| O11 | **Maker rebate formula**: per-market pool, weight p(1−p) | Strategy choice: rebates come from mid-price fills; 0.99 bids get ~nothing | Score market-maker paper bots with the real formula: share = our fee-equivalent / market's total fee-equivalent × 20% of that market's taker fees (needs the market's total taker volume per window from the recordings) |
| O12 | **Taker Rebate tiers** (crypto weight 2.3; Bronze 3% at $2k wV, Silver 8% at $20k) | Small: e.g. $20k wV/month → 8% of fees back plus $60 bonuses | Track our 30-day wV; count TAKER_REBATE in bot P&L |
| O13 | **Liquidity rewards** exist on BTC 5m/15m (min 50 shares, 4.5¢ spread) but no pool is active | Large if a pool returns (August had $525k for BTC 5m/15m) | Poll `/rewards/markets/multi` daily and alert when `clobRewards` appears on crypto Up/Down |
| O14 | **GTD expiry** (−60 s buffer, ≥ 3 min ahead) | Safety: resting orders expire by themselves if the bot dies | Use GTD with `expiration = window_end + 60` for bids placed ≥ 2 min before the end |
| O15 | **Heartbeat semantics**: per key, no disarm, 10 s + 5 s scan, mixing legacy/modern resets IDs | Avoid self-inflicted cancel-alls | One heartbeat sender per API key; never let a test script send one heartbeat with the bots' key |
| O16 | **Session keys** (CLOB-scoped, ≤ 180 days) | Security: the Dublin server would not hold the owner key; revoking a key cancels only its orders | Move server bots to a session key per bot or per host |
| O17 | **No self-trade prevention** on the prediction CLOB | Avoid accidental wash trades between our own bots on one wallet | Cross-check our own resting orders before sending a taker order in the same market |
| O18 | **Markets open ~24 h early** | Possible thin books far ahead of the window | Optional study: are pre-window books stale/profitable for makers |
| O19 | **Ghost fills / async settlement** | Avoid selling shares that are not settled | Sells and merges wait for CONFIRMED (or balance), not `size_matched` |
| O20 | **Deposit-wallet open-order lookup bug** (ts-sdk #364) | Reconciliation | Reconcile from the user channel and our own records; do not trust an empty `get_orders` |
| O21 | **Market WS 1013 "slow consumer"** reported on BTC 5m | Feed reliability | Log close codes/gaps; resubscribe with backoff; split tokens across sockets |
| O22 | **Sports/other categories** have different delays (3 s, 1 s tests) and fees | Only if we expand | — |

## Things that do not help us (checked)

* RFQ / Combos: not enabled on BTC Up/Down (`rfqEnabled false`, `comboStatus disabled`).
* Builder Program: for routing other users; excludes co-location.
* Holding rewards (4% APY): disabled on BTC 5m.
* Referral program: needs other real users; self-referral forbidden.
* Market WS `level: 3`: no visible difference in a 12 s test.

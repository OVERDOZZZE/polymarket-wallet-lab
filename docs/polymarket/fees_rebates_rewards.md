# Fees, maker rebates, taker rebates, liquidity and holding rewards

Read 2026-10-27. Tags as in trading_rules.md.

## 1. Taker fee

`fee = C × feeRate × p × (1 − p)` (C shares, p price). **Makers never pay.** Fees are set by the protocol at match
time; orders carry no fee field since V2 (2026-04-28) [official] trading/fees.

| Category | feeRate | Maker rebate share |
|---|---|---|
| **Crypto** | **0.07** | **20%** |
| Sports | 0.05 (from 0.03 on 2026-07-10) | 15% (from 25%) |
| Finance, Politics, Mentions, Tech | 0.04 | 25% |
| Economics, Culture, Weather, Other | 0.05 | 25% |
| Geopolitics | 0 | — |
| Combos | 0.05 | 0% |

* Live BTC 5m/15m/4h/hourly/daily, 2026-10-27: `feeSchedule {rate 0.07, exponent 1, takerOnly true, rebateRate 0.2}`,
  `feeType crypto_fees_v2`, `makerRebatesFeeShareBps 10000` [ours, Gamma]. Read fees from `feeSchedule`
  (changelog 2026-03-31) [official].
* Peak at p = 0.5: 0.07 × 0.25 = **$0.0175/share (1.75¢; 3.5% of a $0.50 stake)**. At 0.99: $0.000693/share [official table].
* Rounded to 5 dp; minimum fee 0.00001 USDC, so tiny trades near 0/1 can be fee-free [official].
* Trade events show `fee_rate_bps: "0"` even on fee markets (fee applied at match) [ours, market WS 2026-10-27].
* No deposit/withdraw fees from Polymarket [official].

## 2. Maker Rebates Program

[official] programs/maker-rebates:
* Pool = rebate share (crypto 20%) of taker fees collected **in that market**. Since 2026-02-11 "Totals are calculated
  per market, so you only compete with other makers in the same market" — every 5-minute window is its own pool.
* Distribution "fee-curve weighted": for each filled maker order `fee_equivalent = C × 0.07 × p × (1 − p)`;
  `rebate = your_fee_equivalent / total_fee_equivalent × rebate_pool`.
* Paid **daily in pUSD**, minimum accrued **$1** (help center still says USDC). Shown as `MAKER_REBATE` in activity.
* Read: `GET https://clob.polymarket.com/rebates/current?date=YYYY-MM-DD&maker_address=0x...` (no auth; `null` when none);
  SDK `get_current_rebates()` (0.14.0).
* Fee holidays (fee 0) earn no rebate.

What it means for our bots:
* A fill at 0.50 earns ~0.20 × 1.75¢ = 0.35¢/share if our share of the market's fee-equivalent equals our share of the
  takers' fees. A 0.99 fill earns ~0.014¢/share: **late-fav earns essentially no rebate**.
* Because weights follow p(1−p), mid-price liquidity collects most of the pool. A market maker that is filled only
  near the extremes gets a small share even with big size.

## 3. Taker Rebate Program (what `TAKER_REBATE` rows are)

[official] programs/taker-rebates, live since **2026-05-28**:
* Weighted Volume: `wV = trade size ($ = shares × price) × (1 − entry price) × category weight × bonuses`.
  **Crypto weight 2.3** (Sports 1.0; Politics/Finance/Mentions/Tech 1.3; Econ/Culture/Weather/Other 1.7; Geo 0).
  Only fee-paying taker trades count; maker fills never count.
* Tier = 30-day wV, recomputed daily at 00:00 UTC; applies to trades **from then on**, no backfill; drops after a
  "short grace period".

| Tier | 30-day wV | Rebate (of the taker's fees) | One-time bonus |
|---|---|---|---|
| Bronze | $2,000 | 3% | $10 |
| Silver | $20,000 | 8% | $50 |
| Gold | $200,000 | 18% | $250 |
| Platinum | $1,000,000 | 32% | $1,500 |
| Diamond | $4,000,000 | 44% | $7,500 |
| Obsidian | $10,000,000 | 50% | $25,000 |

* Paid daily at midnight UTC in pUSD, $1 minimum; tier upgrade notification `type 11` on `GET /notifications`.
* Abuse (wash trading, self-matching) can remove rebates and tier. Omnibus third-party wallets are excluded.
* Example: $1,000 of BTC taker buys at 0.60 → wV = 1000 × 0.40 × 2.3 = $920. Bronze needs ~$2.2k of such buys per
  30 days; Silver ~$22k.

## 4. Liquidity Rewards

[official] programs/liquidity-rewards, market-data/market-details:
* Per market: `rewardsMinSize` (shares), `rewardsMaxSpread` (cents from the size-cutoff-adjusted midpoint),
  dated `clobRewards` allocations with `rewardsDailyRate`.
* Score per order `S = ((v − s)/v)² × b` (v max spread, s distance, b in-game multiplier). Two-sided: `Q_min =
  max(min(Q1,Q2), max(Q1/c, Q2/c))`, c = 3.0, when the midpoint is in [0.10, 0.90]; **outside that range only
  two-sided liquidity scores**. Book sampled **once per minute at a random offset** (≤1,440 samples per UTC day).
* Paid daily at 00:00 UTC; days below $1 are not paid and do not roll over [official help center].
* Some programmes require orders to rest ≥ 3.5 s to count (March Madness note, changelog 2026-03-17) [official].
* Check whether an order is scoring: `GET /order-scoring`, `/orders-scoring` [GitHub SDK].
* BTC 5m/15m today: `rewardsMinSize 50`, `rewardsMaxSpread 4.5`, **`clobRewards: null` (no active pool)**;
  `clob-markets` shows `"r":{"mi":50,"ma":4.5,"moas":30}` (`moas` undocumented) [ours, 2026-10-27].
* The **$1M "Crypto TWAP Rewards"** for 5m/15m/4h in August 2026 (BTC 5m $300k, BTC 15m $225k, BTC 4h $50k) has ended [official].
* Discover pools: `GET /rewards/markets/multi?q=...&order_by=rate_per_day` (no auth) [official].
* "Sponsored" rewards: third parties can fund a market's rewards (help "Sponsor Market Rewards"; earnings split
  `native_earnings`/`sponsored_earnings`) [official].

## 5. Holding Rewards

**4.00% annualised** on total position value in eligible markets, sampled randomly once an hour, paid daily
(`YIELD_PAYOUT` notification type 7) [official] concepts/positions-tokens. **BTC 5m: `holdingRewardsEnabled: false`** [ours].

## 6. Referral Program (from 2026-05-28)

10% (direct) / 5% (indirect) of **net** fees (after the referee's taker rebate), for 30 days or until the referee
reaches Platinum; needs $10,000 lifetime volume; self-referral and linked accounts forbidden [official] programs/referral-program.
Not useful for us unless we refer real other traders.

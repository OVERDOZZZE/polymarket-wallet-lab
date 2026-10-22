# BTC 5m Up/Down: full market study (2026-10-22)

**Data:** 719 validated windows (0 resolution mismatches), 2026-10-19 17:50Z to 2026-10-22 09:10Z, about 60 hours.
That covers 1.03 M trades and $17.2 M traded on our own feed, plus **3.17 M wallet-level fills from 11,544 wallets**
(Polymarket data API, taker and maker side). Every window is complete, the fills sum to zero in every window, and our feed's trade side
matches the API's taker side on 100% of 1.03 M transactions.

**Method: patterns had to survive fresh data.**
- **Period A** = the 386 windows the earlier analysis (2026-10-21) found its patterns on.
- **Period B** = the 333 windows since 2026-10-21 04:10Z, never looked at before.
- A pattern counts only if it holds in B.
- Confidence intervals resample whole windows: each window is one coin flip, so trades inside it are not independent.
- All taker results include the taker fee (0.07 × p × (1 − p) per share). Maker rebates are excluded (upper bound shown separately).
- Many cells were tested, so a single "significant" cell means little. Only patterns that hold in both A and B are reported as findings.

Raw output: `analysis/out/ult_*.txt` (`python -m analysis.ultimate`, see the end).

---

## 1. The verdict in one paragraph

The market is hard to beat, and **the fee is the wall**: takers paid $343 K in fees over 60 hours (about 2% of what they traded)
and lost $367 K in total. Before fees, takers and makers roughly break even.

Each of these was tested and **failed on fresh data**:
- price-pattern rules (underdogs, favourites, time of window)
- our fair-value model v3
- an ML model (previous study)
- copying the best wallets

Two edges hold up in both periods:

1. **Latency taking.** Buy the side a fast BTC move favours, within ~300 ms. It is real but decays to zero by ~1 s, it varies a lot with
   competition, and our live attempts were too slow to capture it.
2. **Late-favourite maker bids.** Resting bids at 95–99¢ on the near-certain side in the last ~2 minutes earn ~+0.9% per $1. That is small
   per trade but turns over every 5 minutes. It is a maker trade: as a taker the same trade earns nothing.

Everything else we run loses money or is noise.

---

## 2. Is the market price right?

- **Market mid vs model.** The market beats our model v3 at every horizon except the last ~10 s.
  - Out of sample (B), v3's log loss is 0.05 worse with 2–4 minutes left (CI excludes 0).
  - In the last 10 s v3 is better (it knows the averaging window), but there is almost no liquidity left to trade on it.
- **Final minute: the market is too timid.** A logistic recalibration slope above 1 means prices sit too close to 50%.
  - 10–30 s left: b = 1.33 (A) and 1.45 (B).
  - 30–60 s left: b = 1.18 (A) and 1.33 (B).
  - Favourites win more often than priced at the end. This is what feeds the maker edge in section 6.
  - Earlier in the window b ≈ 1 (fair).
- **Long shots are overpriced, but only slightly.**
  - The cheap side at ≤ 3¢ wins 0.7¢ less often than its price (CI excludes 0 in A and in B).
  - At 3–12¢: −1 to −2¢ (A+B).
  - Too small to take as a taker after fees and spread.

## 3. Who wins and who loses (from 11,544 wallets)

| wallet type | wallets | share of volume | net per $1 (after fees, before rebates) |
|---|---:|---:|---:|
| maker bots (≥ 30% of windows, ≥ 70% maker) | 316 | 34% | **+0.5%** (+ up to 0.4% rebates) |
| mixed bots | 78 | 12% | −0.4% |
| taker bots | 352 | 18% | −0.8% |
| regular humans (20+ windows) | 3,460 | 30% | **−2.6%** |
| occasional (< 20 windows) | 7,338 | 5% | **−2.8%** |

- Makers as a whole made +$34 K in B before rebates; takers lost $213 K in B after fees.
- **Skill or luck?**
  - Return per $1 in A barely predicts B (Spearman +0.08 over 977 wallets). For wallets above $20 K volume it is +0.26: some persistence among large bots.
  - **The best 20% of wallets in A lost 5.5% per $1 in B.** Most "winners" were lucky.
- **Copying the 20 best taker wallets of A in B loses 25% per $1**, at any delay from 0 to 2 s. Their own B result was −28%. Do not copy-trade.
- **The consistent winners are almost all maker bots:**
  - two-sided quoters: ply-pm0 ($548 K volume, +1.6%), CrouchPredictions (+1.3%), machinelearned (+3.7%)
  - a family of ~10 identical `0x…-178…` bots at ~$230 K each, with results from −$11 K to +$11 K per instance (same strategy, so the spread is variance)
  - "THG.": buys the 99¢ side late, +1.0% in each period on $563 K

## 4. What takers should never do (both periods, CI excludes 0)

- **Buy at ≤ 5¢:** −67% (A), −53% (B) per $1. Lottery tickets.
- **Buy 50–65¢ favourites:** −4.8% (A), −7.5% (B). 65–80¢: −5% in both, borderline. Mid-priced favourites are the worst trade on the board.
- **Trade tiny sizes (< $1):** −7 to −10%.
- **Trade on model disagreement:** "v3 sees ≥ 10¢ edge after fee" gives −3% (A) and −4% (B).
- The earlier "moderate underdogs 20–50¢ are underpriced, +5 to +10%" was before fees and without window clustering. After fees: +3.8% (A, CI includes 0)
  and −0.1% (B). **It does not exist.** The same goes for every simple price-band or time-band rule tested (`ult_backtest.txt`).

## 5. Edge 1: latency (the only taker edge)

**How it works:** Polymarket's book reprices a BTC move with a lag. After a ≥ 2 bps Coinbase move in 1 s, the Up mid moves
1.9–2.1¢ in the first 300 ms, 2.8–3.5¢ by 500 ms and 3.6–4.9¢ by 1 s. A taker who buys the favoured side before that
gets a stale price.

**Return per $1, held to settlement, fee included** (fill at our view of the best ask after L ms, ≥ 5 shares on top):

| trigger | 0 ms | 300 ms | 500 ms | 1 s |
|---|---|---|---|---|
| Coinbase ≥ 2 bps, A | +8.2% | +4.4% | +2.4% (CI includes 0) | +1.3% (CI includes 0) |
| Coinbase ≥ 2 bps, B | +10.1% | +5.8% | +3.0% (CI includes 0) | +0.8% (CI includes 0) |
| Binance futures ≥ 2 bps, B | +15.3% | +9.8% | +6.7% | +3.5% (CI includes 0) |

The edge is unstable:
- **It varies a lot over time.** In B's first half it was ~0 at 500 ms (Coinbase +0.2%, Binance +1.7%); in the second half +8% and +16%.
- **By hour** (Coinbase, 300 ms): **18–24 UTC +18.8%** [+12, +25], positive on all three days. Other hours: +3 to +5%.
  - Fast-buy volume in that block is about a quarter of the 12–18 block's, so there is less competition.
  - Post-hoc finding: treat it as a hypothesis.
- **Nobody dominates it.** Buys in the move's direction within 1 s came from 1,764 wallets ($483 K, +3.7% per $1). The largest
  (Bonereaper, 7% of it, +8.4%) reacts at a median 327 ms.

Why it has not worked for us with real money:
- Our real FAK orders take 344–406 ms to answer and are matched ~450 ms after the decision. That is where the edge is already ~half gone.
- Our book view itself lags Polymarket by ~70 ms, and much more in spikes.
- Live fast_player_hold: 8 windows, **−$12.76**; 22 of 31 orders found nothing at their limit. Its paper twin made +$4.
- Paper fast_player_hold is +$243 (t = 2.1). Paper fast_player is +$82, t = 2.8: the only paper strategy significant on its own.
  Both assume a 300 ms fill on a book we see late, so they overstate what live trading gets.

**What it would take:**
- (a) Cut reaction time under ~250 ms: a server close to Polymarket's matching engine (measure the ping first), pre-signed orders.
- (b) Use Binance futures as the trigger (allowed: timing signal only). In B it beat Coinbase at every delay.
- (c) Possibly trade mainly 18–24 UTC.

Without (a), expect ≈ 0 after costs.

## 6. Edge 2: late-favourite maker bids (new, robust)

**Maker BUY fills on the near-certain side** (all wallets' real fills, so the losses from being filled at bad moments are already included):

| price | A per $1 | B per $1 |
|---|---|---|
| 95–97¢ | +1.7% [+0.2, +2.9] | +1.6% [+0.0, +2.9] |
| 98–99¢ | **+0.95%** [+0.8, +1.0] | **+0.84%** [+0.5, +1.0] |
| 99–100¢ | +0.15% | +0.12% |

**By time left (≥ 95¢, A+B):**
- 0–30 s left: +0.8%
- 30–60 s left: +1.1%
- 60–120 s left: +0.9%
- more than 2 minutes left: not significant

**Risk:**
- 1.5% of these fills lost.
- 23 of 719 windows lost money in total; the worst window lost $4.9 K across all makers. Profits are steady and losses rare but large.
- In total, $43 K profit on $5.85 M of fills: about $60 per window for everyone together.

**The taker version earns nothing.** Buying the same favourite at the ask with the fee: −1% to +1.6%, mostly CI including 0.
The profit is in waiting for sellers, not in crossing the spread.

**Who pays:** sellers dumping winning shares early at 95–99¢. Our own 99¢-sell rule is one of them: it lost $12.99 (fast_player),
$15.32 (fast_player_hold) and $3.28 (fast_player_kelly) vs holding.

**Why it suits a small account:** capital is tied up for only ~2–5 minutes per window, so even ~1% per window adds up.

**Unknowns we cannot measure from these fills:**
- Our place in the queue. Many bots sit at 98–99¢; a late-in-queue order gets filled mostly when a big sell sweeps through, which is when reversals happen.
- So the first step is a **paper strategy with the conservative fill rule** (fill only on trades *through* our price), then compare it with these numbers.

## 7. Our bots (paper ledgers, per-window t-statistics)

| strategy | windows | PnL | per $1 | t |
|---|---:|---:|---:|---:|
| fast_player | 253 | +$81.8 | +3.2% | **2.8** |
| fast_player_hold | 247 | +$243.4 | +9.6% | 2.1 (41% of profit from its 5 best windows) |
| fast_player_livesim | 104 | +$87.4 | +11.0% | 1.5 (76% from 5 windows) |
| fast_player_kelly | 128 | −$3.4 | −0.4% | −0.1 |
| fast_player_value | 16 | −$1.1 | | too early |
| maker_v2 | 275 | −$59.6 | −0.7% | −0.2 |
| maker / maker_v3 | 68 / 19 | −$98.8 / −$39.7 | | −1.9 / −1.5 |
| copy_maker / _guarded | 22 / 10 | −$100.0 / −$100.0 | | wiped out |
| late_taker / _v2 | 38 / 40 | −$66.3 / −$36.7 | | **−4.2** / −1.7 |
| contrarian / leader / open_drift / early_underdog / misread_fader | | all negative | | −0.4 to −2.0 |

Real money:
- Live maker_v2: −$18.61 over 56 windows.
- Live fast_player_hold: −$12.76 over 8 windows.
- Manual page: −$34.23 over 24 settled windows.

Our makers lose for the same reason human takers do: slow quotes get picked off. Maker fills at 10–35¢ lost 30–36% (previous study).
The winning maker bots requote in milliseconds and collect rebates on very large volume.

## 8. Recommendations

1. **Stop** what is negative on both data sets: late_taker, late_taker_v2, contrarian, leader, early_underdog, maker, maker_v3,
   copy_maker(_guarded), and misread_fader/open_drift (negative, no edge in the market data). Keep their history.
2. **Turn off the 99¢-sell rule.** It sells into the one robust maker edge and has lost money vs holding for every bot.
   This changes the setup mid-evaluation, so restart the affected evaluations.
3. **Add a paper strategy `late_fav_maker`:**
   - in the last 120 s (stop ~5 s before the end), when one side's mid is ≥ 0.97, rest a post-only bid at 98¢ (and/or 95–97¢) on that side
   - hold to settlement
   - conservative and optimistic fills, as usual
   - Judge it on fresh data (≥ 300 fills or 5 days) against the +0.9% per $1 above.
4. **Latency:** add a Binance-futures-triggered fast_player variant (paper, live-style fills). Before any more real money:
   - measure the ping from a VPS near Polymarket
   - do not run the live bot from this PC/Wi-Fi
5. **Do not:** buy long shots, buy 50–80¢ favourites as a taker, trade model-vs-market disagreements, or copy other wallets.

## 9. Caveats

- 60 hours of one market regime (BTC 1 s volatility ~0.45 bps). Edges in this market are known to shift (latency edge: ~0 to +16% between half-days).
- The latency numbers fill at the best ask in *our* view of the book, which lags Polymarket by ~70 ms and more in spikes.
  They assume nobody else takes that size first. They are upper bounds; live fills were far worse.
- Maker results are measured on other traders' fills. Our own queue position is unknown until we paper-test.
- Rebates are an upper bound (20% of the fee-equivalent) and excluded from every PnL here.

## Reproduce

```
.venv-analysis\Scripts\python -m analysis.build_panel      # panel, trades, 100 ms grid (+ sizes, Binance futures)
.venv-analysis\Scripts\python -m analysis.fetch_wallets    # wallet fills from the data API (cached per window)
.venv-analysis\Scripts\python -m analysis.ultimate         # all sections -> analysis/out/ult_<section>.txt
```
The hour-of-day split in section 5 was an ad-hoc run (Coinbase >= 2 bps, by UTC 6-hour block).

# Imbalance maker: build description

A market-making bot for Polymarket's BTC Up/Down 5-minute windows, built from the reverse-engineering of
CrouchPredictions (and a look at machinelearned) in this project. This file says exactly what the bot does, why,
what it is expected to earn, and how to add it to the existing app (`Polymarket BTC Analysis`).

The decision logic is also code: [`pmre/strategy.py`](../pmre/strategy.py) (`QuoterConfig`, `Quoter`,
`BinanceGuard`, `rule_size`; its defaults are the final configuration). The final backtest
([`pmre/bot_sim.py`](../pmre/bot_sim.py)) takes every decision from that code, and reproduces the research backtest
fill for fill, so the numbers below are for exactly this logic. Port it rather than re-deriving it from the prose.

## For the Claude Code session implementing this in the app (read first)

You are working in `C:\Users\faust\Desktop\Projects\Polymarket BTC Analysis`. This spec and its reference code live
in `C:\Users\faust\Desktop\Projects\Polymarket Trader Analysis` (`reports/BOT_SPEC.md`, `pmre/strategy.py`); read
both before writing anything. Follow the app's own `CLAUDE.md` and `STRUCTURE.md` (update STRUCTURE.md when you add
the strategy).

**Phase 1, now: a paper version only.**
* Build it exactly as specified in sections 3-5: strategy `imb_maker` (`ImbMakerConfig(GuardMakerConfig)`,
  `ImbMakerBot(GuardMakerBot)`), registered in `backend/bots/registry.py`, paper mode. Port the decisions from
  `pmre/strategy.py` (`Quoter.top_depths`, `Quoter.desired`, `BinanceGuard`, `rule_size`) with the final defaults:
  B 60, 13 levels, floor 0.10, inventory cap 300, quote from start + 5 s to end - 25 s, join at queue share >= 0.4
  else 2 ticks back, Binance futures guard 0.5 bps / 1000 ms / 1000 ms cooldown, BTC 5m windows only.
* Do not add anything the spec does not have: no fair-value or model filter (`maker_min_edge=-1`), no Coinbase jump
  pull (`jump_bps=1e9`), no inventory skew, no takes, no spot feeds in the guard (`sources=("binance_fut",)`). If
  the engine forces a behaviour that differs from the spec, say so to the user instead of silently adapting.
* Paper timing: `latency_ms=35`, `cancel_latency_ms=35` (measured from Dublin). Never 0: that makes a maker look
  better than it is.
* Log, per window, every order (side, price, size, send time, time live, cancel send time, cancel effective time),
  every fill (time, price, size, which order), every guard trigger (Binance move, side pulled) and, at each decision,
  bb, ba, size at bb, size at ba, the two queue shares and the chosen depths. These logs are needed later to compare
  the bot's real fills with this project's simulator.
* Check the port against these cases from `pmre/strategy.py` (Up-terms bb / ba; prices below in each token's own
  terms; sizes = floor(60 / (1 - |p - 0.5|)); t well inside the window, best prices set 5 s earlier):

  | case | bb | ba | size at bb | size at ba | net | Up bids | Down bids |
  |---|---|---|---|---|---|---|---|
  | A | 0.48 | 0.49 | 300 | 200 | 0 | 13 levels 0.48 x 61 .. 0.36 x 69 | 13 levels 0.51 x 60 .. 0.39 x 67 |
  | B | 0.48 | 0.49 | 50 | 450 | 0 | 0.46 x 62 .. 0.34 x 71 (2 back) | 0.51 x 60 .. 0.39 x 67 |
  | C | 0.48 | 0.49 | 150 | 350 | 0 | 0.46 x 62 .. 0.34 x 71 (share 0.3) | 0.51 x 60 .. 0.39 x 67 |
  | D | 0.48 | 0.49 | 300 | 200 | +350 | none (cap) | 0.51 x 60 .. 0.39 x 67 |
  | E | 0.48 | 0.49 | 300 | 200 | -350 | 0.48 x 61 .. 0.36 x 69 | none (cap) |
  | F | 0.15 | 0.16 | 500 | 500 | 0 | 6 levels 0.15 x 92 .. 0.10 x 100 | 13 levels 0.84 x 90 .. 0.72 x 76 |
  | G | 0.48 | 0.49 | 0 | 0 | 0 | as A (share 0.5) | as A |

  Before start + 5 s and from end - 25 s: no orders. Guard: futures last price 100000.0 received at t - 1500 ms and
  t - 1200 ms, then 99994.0 at t (-0.6 bps) -> pull every Up bid at t, Up stays unquoted until t + 1000 ms, Down
  keeps quoting; a +0.4 bps move pulls nothing.
* When done, tell the user how to start it, where its logs are, and any place where the app could not follow the
  spec exactly.

**Phase 2, only when the user explicitly asks for it: a live version.** Do not place live orders, arm trading or
build the live path before that request (the app's CLAUDE.md says the same). When asked:
* Reuse the app's `Gateway` (post-only GTC BUY orders, cancels, user-channel fills), the same decision code as the
  paper version, and a kill switch / loss limit like `fast_player`'s live mode.
* First live test settings (scaled ~12x down): **B = 5** (5-8 shares per order, above the 5-share minimum),
  **inv_cap = 25**, a per-window spend cap of about **$100** (`max_window_cost`), bankroll **$300-400**. Keep the
  paper bot running on the same windows next to it.
* Keep logging exactly as in phase 1 for the live orders (plus the exchange's acknowledgement times), so the live
  fills can be compared with the paper bot and with this project's simulator before any size increase.
  Scale-up path, each step only on the user's request and only if the live cents per share hold up: B 5 ($300-400)
  -> B 20 (~$1.5-2k) -> B 60 (~$5k, the full specification).

## Bottom line

* In a simulator calibrated on CrouchPredictions' own orders, at the order latency measured from the Dublin server
  (40 ms from a book event to our order being live), the bot earns **+$3.3 per window on the 60 s markout
  (± 2.9), +$2.2 on settlement (± 3.4), about $600-950 per day** over 658 windows on 2026-10-22, 23 and 24.
  **Almost all of it is the maker rebate** (~$900/day): the trading itself is about break-even.
* That is about one standard error from zero: plausible, not proven. Three recorded days are all there is.
* CrouchPredictions' own orders score +$4.7 per window in the same simulator (at its real timing). The simulator
  is pessimistic: it scores CrouchPredictions' real fills 1-2.5 c/share below what they really earned. If the bot's
  real fills beat the simulation the same way, it would trade at a profit like CrouchPredictions (real: ~$1.5-3.7k
  per day on 5m maker fills plus ~$1k/day rebates). Only paper and then small live trading can tell.
* **Speed matters more than anything else.** Each extra 10-20 ms of order latency costs about $1-5 per window
  depending on the configuration; the final configuration was chosen because it degrades least (+3.3 at 40 ms,
  +2.3 at 50 ms; the same rule with the top only one tick back when not joining: +6.1 at 25 ms, +0.8 at 40 ms,
  -4.4 at 60 ms). Dublin is
  already the closest location allowed to place orders (Polymarket's servers are in London, ~23 ms away; the UK is
  close-only), so keep the decision path in-process and fast.

## 1. What the bot does, in one paragraph

In every 5-minute BTC Up/Down window it rests post-only BUY orders on both tokens (Up bids and Down bids): a ladder
of 13 orders per token, 1 c apart, sized by CrouchPredictions' rule. The top of each ladder joins the token's best
bid only when the resting size at that best bid is at least 40 % of the size resting at the token's two best prices
(its best bid and its best ask); otherwise it sits two ticks behind the best bid. When Binance BTCUSDT perpetual
futures moves 0.5 bps or more against a token within one second, every bid on that token is cancelled at once and
the token stays unquoted for one second after the last such move. It never takes liquidity, never sells, holds to
settlement (or merges pairs), and earns the spread between the two tokens' bids plus the maker rebate.

## 2. Why each rule is there (evidence)

* **Ladder, size rule, both sides, hold to settlement**: CrouchPredictions' observed structure (NOTES.md "Crouch:
  quoting reconstruction"): ~13 contiguous whole-cent levels per token, size = floor(B / (1 - |p - 0.5|)) with
  B = 60 since 2026-10-21, buys only, about 850 Up and 850 Down shares per window at an average pair cost ~0.94.
* **Binance futures guard**: in the simulator, a plain ladder loses (2 c behind the mid on each side: -1.13 c/share
  on the 60 s markout, -$5.3k/day incl. rebates). Almost all of it is fills taken within ~1 s after Binance futures
  moved against the token: the Polymarket book catches up with Binance over seconds, and the move is visible in
  Dublin >= 200 ms before those fills. Fills after an adverse move >= 0.5 bps: -2 to -12 c/share; in quiet periods
  ~0. Futures trades lead spot (the same guard on spot book ticks: -$0.4k/day on 10-23, futures: +$1.6k). 0.5 bps
  is the best of 0.3 / 0.5 / 1 / 2 bps; window 0.5-2 s and cooldown 1-3 s are within noise; a 60 ms instead of
  30 ms guard cancel changes nothing measurable.
* **Queue-share gate on the top**: fills at the best are profitable when the queue at the token's best bid is large
  against the queue at its best ask, and lose when it is thin (share < 0.15: -0.4 to -1.6 c/share; share >= 0.5:
  +0.5 to +2.3), on all three days; this is what CrouchPredictions does (it joins big, established best levels).
  Thresholds 0.3-0.5 perform alike; 0.6 is worse; requiring the best price to be >= 0.15 s old hurts.
* **Two ticks back when not joining**: one tick behind the best is where the fills lose at realistic latency (the
  best empties, the order becomes the best and is hit before it can follow). Two back: +3.3 vs +0.8 per window at
  40 ms. A three-tier variant (1 tick back, 2 when the share < 0.15) is equivalent (+3.0 at 40 ms, +2.3 at 50).
* Tried and dropped: a fitted model of CrouchPredictions' on/off and depth decisions (does not transfer: -$5.1k/day),
  a guard on the Polymarket book itself (fewer fills, no better per share), a gradient-boosted fill-quality model
  (overfits: fills of a window share one outcome), price-dependent quoting (favourite/longshot effects flip sign
  from day to day), stopping 60 s before the end.

## 3. Exact logic

Units: prices in Up terms as integers px = price x 1000 (1 c = 10). Up bid at px = BUY Up at px/1000. Down bid at
Down price q = Up-terms px 1000 - 1000 q (it is an Up ask). bb / ba = best Up bid / best Up ask in the Up-terms book
(ba = 1 - best Down bid, because the two books mirror each other).

Per window (5-minute BTC Up/Down, slug btc-updown-5m-<start>):

1. **Clock**: quote from start + 5 s to end - 25 s. At end - 25 s cancel everything; no orders outside.
2. **Book state** (from the CLOB market channel of both tokens): bb, ba, the resting size at bb (`size_bb`) and at
   ba (`size_ba`), excluding our own orders.
3. **Queue share**: `share_up = size_bb / (size_bb + size_ba)`, `share_down = 1 - share_up` (0.5 if both are 0).
4. **Top of each ladder** (ticks behind the token's best bid): 0 if share >= `imb_join` (0.4), else `back_ticks` (2).
   Up top = bb - depth x 10; Down top (Up terms) = ba + depth x 10, i.e. Down price = (1 - ba) - depth x 0.01.
5. **Ladder**: from the top, `n_levels` (13) levels 1 c apart going away from the market (Up: top, top-10, ...;
   Down: top, top+10, ... in Up terms), stopping below 0.10 in the token's own price (`floor_px` = 100). Size of each
   level = floor(60 / (1 - |p - 0.5|)) (p = the token's price; 60 @ 0.50, 66 @ 0.40, 75 @ 0.30, 100 @ 0.10).
6. **Inventory cap**: net = Up shares bought - Down shares bought in this window. No Up bids while net > 300; no Down
   bids while net < -300 (`inv_cap`).
7. **Binance guard**: on every Binance USD-M BTCUSDT futures trade (aggTrade, last price `p`), as received:
   r = price / (last price received at or before now - 1000 ms) - 1. If r <= -0.5 bps: cancel every Up bid now and
   keep Up unquoted until now + 1000 ms (extended by later triggers). If r >= +0.5 bps: the same for Down.
8. **Order management**: on every book change at either best (price or size), every guard expiry and every own fill
   (and a 1 s heartbeat), compute the desired set {(side, px): size}. Cancel resting orders whose (side, px) is not
   in it; place the missing ones (post-only GTC BUY). Leave orders that are still wanted alone: their queue position
   is what earns the fills. No amend, no cancel-and-replace of a wanted order.

Reference: `Quoter.desired(t, bb, ba, size_bb, size_ba, t_bb, t_ba, net)` returns the set of step 8;
`Quoter.on_binance(recv_ms, price)` returns the side to cancel at once (step 7).

## 4. Parameters

| name | final | tested | meaning |
|---|---|---|---|
| B | 60 | 60 | size rule base (CrouchPredictions' current) |
| n_levels | 13 | 13 | ladder levels per token |
| floor_px | 100 | 100 | no bid below 0.10 |
| inv_cap | 300 | 300 | net-share cap per window |
| start_s / stop_s | 5 / 25 | 5 / 25, 60 | quoting window inside the 300 s |
| imb_join | 0.4 | 0.3, 0.4, 0.5, 0.6 | queue share needed to join the best bid |
| back_ticks | 2 | 1, 2 | ticks behind the best when not joining |
| imb_low / back_low | off | 0.15 / 2, 0.15 / 3, 0.3 / 2 | optional third tier (equivalent results) |
| age_join | 0 | 0, 0.15 s | minimum age of the best price to join (hurts) |
| guard_bps | 0.5 | 0.3, 0.5, 1, 2 | Binance futures move that pulls a side |
| guard_win_ms | 1000 | 500, 1000, 2000 | move window |
| guard_cool_ms | 1000 | 1000, 2000, 3000 | side stays out this long after the last trigger |
| order latency | 40 ms | 25, 40, 50, 60 | book event (exchange time) -> our order live / cancelled |
| guard latency | 30 ms | 30, 60 | Binance tick received -> cancel effective |

## 5. Implementing it in the existing app

The app's `guard_maker` strategy (`backend/bots/strategies/guard_maker.py`) already has the guard (its `sources`
include `binance_fut`, the futures last trade; the move is measured exactly as in step 7) and the PaperBot engine
(`market_maker.py`) has the order reconciliation (`_reconcile` keeps orders at still-wanted prices, cancels the
rest). Only the quote function is new. Suggested: a new strategy file `backend/bots/strategies/imb_maker.py`:

* `ImbMakerConfig(GuardMakerConfig)`: `windows=(5,)`, `sources=("binance_fut",)`, `spike_bps=0.5`,
  `lookback_ms=1000`, `pull_ms=1000`, `combine="mean"`, `jump_bps=1e9` (the engine's Coinbase jump pull was not part
  of the tested logic), `maker_min_edge=-1.0` (no model filter), `min_price=0.10`, `max_price=0.99`,
  `start_delay_s=5`, `stop_before_end_s=25`, `latency_ms=35`, `cancel_latency_ms=35` (Dublin), `min_requote_ms=0`
  or as low as the engine allows, plus `B=60`, `n_levels=13`, `inv_cap=300`, `imb_join=0.4`, `back_ticks=2`.
* `ImbMakerBot(GuardMakerBot)`:
  * `_side_quotes(m, side, fair, now_s, imb)` ignores `fair`: best_bid = m.best_bid(side), best_ask =
    m.best_ask(side) (token terms); size_bid = m.books[side]["bids"][best_bid], size_ask =
    m.books[side]["asks"][best_ask], each minus our own resting size at that price; share = size_bid / (size_bid +
    size_ask) (0.5 if both 0); top = best_bid if share >= imb_join else best_bid - back_ticks x 0.01; return
    [(px, floor(B / (1 - |px - 0.5|))) for px = top - i x 0.01, i = 0..n_levels-1, while px >= 0.10]
    (round px to 2 decimals). Return [] when `_blocked` says so.
  * `_blocked(m, side, imb, heavy, cost_cap)`: `(side == "up" and imb > inv_cap) or (side == "down" and imb <
    -inv_cap)` (imb = Up shares - Down shares, as the engine passes it).
  * Requote triggers: the engine requotes on spot ticks; make sure every book message that changes the price or the
    size at either best of either token requotes that window at once (the share moves with the size).
* Register it in `backend/bots/registry.py` STRATEGIES and run it in paper mode on the Dublin server first.
* Note on the paper engine: it models cancels ahead of us pro rata, this project's simulator rebuilds every order's
  queue (FIFO, per-order cancels) and replays each trade as a taker walking the book. Expect paper numbers to differ
  from section 6. The real test is small live size: compare live fills with what paper said for the same windows.

## 6. Expected results (simulator, final configuration)

60 s markout = value of the bought token on the book mid 60 s after the fill (settlement if the window ended),
minus the price; it has far less noise than settlement. Rebate = 0.2 x 0.07 x p (1 - p) per share (exact for both
traders on every day checked). 10-22 covers 15-17h and 19-23h only (the recording lacks Binance futures 08-14h, 18h).

| day | windows | shares | markout + rebate $/window | settle + rebate $/window | of which rebate $/window |
|---|---:|---:|---:|---:|---:|
| 2026-10-22 | 96 | 100k | -8.1 ± 7.4 | +4.1 ± 8.6 | 2.8 |
| 2026-10-23 | 286 | 300k | +9.8 ± 4.3 | +3.6 ± 4.9 | 2.7 |
| 2026-10-24 | 276 | 387k | +0.6 ± 4.7 | -0.0 ± 5.8 | 3.7 |
| **all** | **658** | **786k** | **+3.3 ± 2.9** | **+2.2 ± 3.4** | **3.1** |

Per day (288 windows): ~340k shares, ~$600 (settlement) to ~$960 (markout), including ~$900 of rebates.
Latency 50 ms: +2.3 ± 3.0 (markout), +1.5 ± 3.4 (settlement) per window.

Same windows, other strategies (markout + rebate, $/window, at 40 ms unless noted): CrouchPredictions' own orders
replayed +4.7; join at share >= 0.4 else one tick back +0.8 (25 ms: +6.1, 60 ms: -4.4); futures guard only, 2 c
behind the mid +0.5 (25 ms); no guard -18.4 (10-23, 25 ms).

Operations, from the same runs: ~1,200 shares bought per window (p90 ~2,600), fill cost ~$700 per window (p90
$1,200, max ~$3,400), net unhedged shares at the window end ~200 (p90 ~360); the two resting ladders hold ~$720-830.
A bankroll of ~$5k covers the worst window plus the resting ladders and positions waiting for redemption.
Order traffic: ~2,400 placements and as many cancels per window (~9 per second each while quoting), about three
times CrouchPredictions' rate; check Polymarket's current rate limits and use batch order / cancel endpoints.

## 7. How the simulator works and what it cannot know

* Every order on the book is rebuilt from the recorded level-by-level feed (every size increase is a new order,
  every decrease a trade or the cancel of one order of that size; FIFO within a level). Our orders are inserted into
  those queues; each trade print is replayed as a taker walking the book with our orders in it (pmre/levelq.py).
* Calibrated on CrouchPredictions' own orders given their real lifetimes: simulated filled shares within ~10 % of its
  real on-chain fills on all three days, per-window correlation 0.84-0.93.
* **It is pessimistic about PnL**: the same replay scores CrouchPredictions' own orders 1-2.5 c/share below what they
  really earned (on 10-24: ~0 vs +1.9 c/share). Whether the bot's real fills are equally better than simulated is
  unknown until it trades.
* Small-participant assumption: our orders do not change what other participants do. With B = 60 (CrouchPredictions'
  size) that is plausible but not guaranteed; another bot on the same prices would share the fills.
* Only BTC 5m windows were simulated (15m and other assets are not covered).
* Three days of recordings; the results are suggestive, not proven (see the standard errors).

## 8. What machinelearned does (for reference)

Its real 5m maker fills (10-23, 10-24: 362k shares, +0.52 c/share markout + rebate) are 69 % at the best, often at
best prices less than 0.15 s old, with larger orders (25-500 shares, 29 % of its volume in fills over 100 shares);
it earns most on its few fills one tick behind (+4.5 c). Its edge does not follow the queue share, its orders
cannot be identified in the book by size, and it earns less than CrouchPredictions (+0.68 c/share, 893k shares on
the same days), so it was not cloned.

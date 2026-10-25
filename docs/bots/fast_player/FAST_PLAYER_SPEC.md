# Fast Player strategies: rebuild spec

Spec for rebuilding three paper-trading strategies from the `btc5m` project on another machine:

| Dashboard name | Strategy id | One line |
| - | - | - |
| Fast player | `fast_player` | Buys the side a Coinbase/Kraken spike favours before Polymarket reprices, then tries to lock a profit by buying the other side |
| Fast player (hold) | `fast_player_hold` | Same entries, held to settlement (one taker fee instead of two) |
| Fast player (live copy) | `fast_player_livesim` | fast_player_hold run the way the real-money bot ran it: minimum-order stake, live-style FAK fills, no risk limits, $125 bankroll |

All three are **paper only** (virtual money). Section 10 covers the real-money twin (`LiveFastPlayerHold`) for reference.

Code in this file is copied from the working project (state as of 2026-10-22). File paths refer to the original layout.

---

## 1. The market and the edge

**Market.** Polymarket "BTC Up or Down 5m": one binary market per 5-minute window. The slug is `btc-updown-5m-{window_start_unix}` (start is a multiple of 300). Two tokens, `Up` and `Down`. Each winning share pays $1.

**Resolution.** Up wins if `finalPrice >= priceToBeat`.
- `priceToBeat` = Chainlink BTC/USD 60 s TWAP at the window start.
- `finalPrice` = the same TWAP at the window end.
- The TWAP stamped `T` is the plain mean of the 60 one-second Chainlink spot points ending 2 s before `T` (36% of the time) or 3 s before `T` (64%).

**The edge (latency).** Chainlink spot is published about 1.3 s after its source timestamp. A Chainlink point stamped `s` tracks exchange prices at `s − 250 ms`. Coinbase arrives in about 34 ms and Kraken in about 70 ms. So when BTC jumps on Coinbase/Kraken, we know where Chainlink, and therefore the settlement average, is heading about 1-2 s before Polymarket's chart shows it. Measured in-sample:
- About 0.7 spikes of ≥ 2 bps per window.
- After a spike the Up mid kept moving in the spike direction by about 0.046 over 3 s. About 0.018 of that happened within 300 ms.
- The Up price completes ~38% of its repricing during the move second, 81% within 1 s and 99% within 2 s.

**Rule for every strategy here.** Exchange prices are used for direction and timing (spike detection) and for the model v3 nowcast, which is a *ratio* applied to the last Chainlink price. The *price level* always comes from Chainlink. Never use a USDT feed (such as Binance) as the price level.

---

## 2. Data feeds needed

| Feed | URL | Use |
| - | - | - |
| Polymarket CLOB book | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Local order book per token (asks walked for fills) |
| Chainlink spot + TWAP (PolyBolt) | `wss://ws-live-v2.polymarket.com/ws` (needs CLOB API key/secret/passphrase) | `priceToBeat`, spot points for the model, sigma |
| Coinbase ticker | `wss://ws-feed.exchange.coinbase.com`, `{"type":"subscribe","product_ids":["BTC-USD"],"channels":["ticker","heartbeat"]}` | Spike composite + nowcast |
| Kraken v2 ticker | `wss://ws.kraken.com/v2`, `{"method":"subscribe","params":{"channel":"ticker","symbol":["BTC/USD"]}}` | Spike composite + nowcast |
| Gamma | `GET https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{start}` | Tokens, `feeSchedule`, `orderMinSize` (5), tick (0.01), outcome. **Add a unique `_=` query param when polling for results**: responses are cached for up to 300 s |

**Exchange feed handlers.** Each feed calls `state.add_exchange(name, recv_ms, src_ms, price)` on every ticker message. Use the **trade price**:
- Coinbase: the `price` field.
- Kraken: the `last` field, with the default trade trigger, not `event_trigger: "bbo"`.

The paper bots use trade prices. The live server once used Kraken bbo, and that changed which spikes fired (see §9).

```python
# btc5m/feeds/coinbase.py (inside the receive loop)
if t != "ticker" or msg.get("product_id") != "BTC-USD":
    continue
src = parse_iso_ms(msg["time"]) if msg.get("time") else None
self.state.add_exchange("coinbase", recv, src, float(msg["price"]))
self.state.mark("coinbase", recv)

# btc5m/feeds/kraken.py
for d in msg.get("data") or []:
    if d.get("symbol") != "BTC/USD" or d.get("last") is None:
        continue
    src = parse_iso_ms(d["timestamp"]) if d.get("timestamp") else None
    self.state.add_exchange("kraken", recv, src, float(d["last"]))
    self.state.mark("kraken", recv)
```

**CLOB hook.** The live-copy fill rule (§8) needs a hook that runs before each CLOB message changes the book:

```python
# btc5m/feeds/clob.py
def handle(self, ev: dict, recv: int) -> None:
    et = ev.get("event_type")
    src = int(ev["timestamp"]) if ev.get("timestamp") else None
    if src is not None:
        for l in self.pre_listeners:          # the engine registers itself here
            l.before_event(ev, src, self.conn_id)   # conn_id increments on every reconnect
    ...apply book / price_change...
```

**Staleness.** Required feeds are `chainlink_spot`, `chainlink_twap` and `clob`. If any of them has no update for more than 3 s, there is no model output and no orders. An exchange counts as "fresh" if it updated within 3 s. Model v3 needs at least one fresh exchange.

---

## 3. Shared state (`btc5m/state.py`)

```python
class State:
    def __init__(self):
        self.last_recv: dict[str, int] = {}
        self.spot_points: dict[int, str] = {}       # Chainlink 1 s points keyed by src_ts ms (exact decimal strings)
        self.twap_points: dict[int, str] = {}
        self.spot_latest: tuple[int, float] | None = None   # (src_ts, price)
        self.exchange_latest: dict[str, tuple[int, float]] = {}   # name -> (recv_ts, price)
        self.exchange_hist: dict[str, deque] = {}                   # name -> (src_ts, price), last ~8000 ticks
        self.exchange_listeners: list = []                          # callables(name, recv_ts, price)
        self.books: dict[str, Book] = {}

    def add_exchange(self, name, recv, src, price):
        self.exchange_latest[name] = (recv, price)
        if src:
            h = self.exchange_hist.setdefault(name, deque(maxlen=8000))
            if not h or src >= h[-1][0]:
                h.append((src, price))
        for cb in self.exchange_listeners:      # fast_player's on_exchange runs HERE, on every tick
            cb(name, recv, price)

    def fresh_exchanges(self, now, stale_after_s):
        return [n for n, (recv, _) in self.exchange_latest.items() if now - recv <= stale_after_s * 1000]

    def exchange_series(self, now, stale_after_s, since_ms):
        """{name: (src_ts list, price list)} for fresh exchanges since `since_ms`, and the latest src_ts."""
        out, until = {}, 0
        for n in self.fresh_exchanges(now, stale_after_s):
            h = self.exchange_hist.get(n)
            if not h:
                continue
            pts = [x for x in h if x[0] >= since_ms] or [h[-1]]
            out[n] = ([t for t, _ in pts], [p for _, p in pts])
            until = max(until, pts[-1][0])
        return out, until

    def exchange_composite(self, now, stale_after_s):
        """Mean of the latest fresh Coinbase/Kraken prices (for move detection)."""
        px = [self.exchange_latest[n][1] for n in self.fresh_exchanges(now, stale_after_s)]
        return sum(px) / len(px) if px else None
```

`Book` holds `bids`/`asks` as `{price: size}` dicts, with `best_bid()`, `best_ask()` and `ask_levels(n)` (sorted ascending; `None` = all levels).

---

## 4. Fair value: model v3 (`btc5m/model.py`, `btc5m/runner.py`)

All three strategies use **model v3** (`uses_v3 = True`).

### 4.1 Core formula (driftless Brownian motion, settlement = 60 s average)

Inputs:
- `S0` = priceToBeat.
- `P` = current spot.
- `r` = spot seconds still to come inside the averaging window.
- `A` = mean of the known spot points inside the window.
- `sigma` = per-second volatility of log returns.

```python
def fair_value(S0, P, r, sigma, A=None, twap_s=60.0, dist="normal", df=4.0):
    W = twap_s
    if r > W:
        mean = P
        std = sigma * P * math.sqrt((r - W) + W / 3)
    else:
        r = max(r, 0.0)
        a = P if A is None else A
        mean = ((W - r) * a + r * P) / W
        std = (r / W) * sigma * P * math.sqrt(r / 3)
    if std <= 0:
        return (1.0 if mean >= S0 else 0.0), mean, 0.0
    z = (mean - S0) / std
    p = 0.5 * (1 + math.erf(z / math.sqrt(2)))      # (student_t option exists, unused)
    return p, mean, std
```

### 4.2 Real settlement window (mixture over the lag k)

```python
def fair_value_settlement(S0, P, spot_ts_s, T, sigma, spot, lag_weights, twap_s=60, dist="normal", df=4.0):
    """TWAP stamped T = mean of the 60 one-second spot points ending at T - k, k from lag_weights {2:0.36, 3:0.64}."""
    total = sum(lag_weights.values())
    p_mix = 0.0
    main = max(lag_weights, key=lag_weights.get)
    out = (0.0, 0.0, 0)
    for k, wgt in lag_weights.items():
        te = T - k                       # last spot second inside the average
        lo = te - twap_s + 1             # first spot second inside the average
        r = te - spot_ts_s               # spot seconds still to come
        known = [spot[t] for t in range(lo, min(te, spot_ts_s) + 1) if t in spot]
        A = sum(known) / len(known) if known and r < twap_s else None
        p, mean, std = fair_value(S0, P, float(r), sigma, A, twap_s, dist, df)
        p_mix += wgt / total * p
        if k == main:
            out = (mean, std, len(known))
    return p_mix, out[0], out[1], out[2]
```

### 4.3 Nowcast of the unpublished Chainlink points (what makes it "v3")

```python
def nowcast_spot(cl_ts_s, cl_price, ex, until_ms, offset_ms=-250):
    """spot(t) ~= cl_price * mean over exchanges of px_ex(t + offset) / px_ex(cl_ts + offset).
    ex: {name: (src_ts_ms ascending, prices)}. Returns (P_now, {second: est spot}, until_s) or None."""
    from bisect import bisect_right
    ref_t = cl_ts_s * 1000 + offset_ms
    refs = {}
    for name, (ts, px) in ex.items():
        i = bisect_right(ts, ref_t) - 1
        if i >= 0:
            refs[name] = px[i]
    if not refs:
        return None

    def ratio(t_ms):
        rs = []
        for name, ref in refs.items():
            ts, px = ex[name]
            i = bisect_right(ts, t_ms) - 1
            if i >= 0:
                rs.append(px[i] / ref)
        return sum(rs) / len(rs)

    until_s = until_ms // 1000
    est = {t: cl_price * ratio(t * 1000 + offset_ms) for t in range(cl_ts_s + 1, until_s + 1)}
    return cl_price * ratio(until_ms + offset_ms), est, max(until_s, cl_ts_s)
```

### 4.4 Volatility: EWMA over the last 30 min of 1 s Chainlink spot returns

Half-life 600 s; needs at least 300 points. On restart, seed it from stored points.

```python
def ewma_sigma(ts_s, prices, halflife_s, now_s, min_points=2):
    if len(prices) < max(2, min_points):
        return None
    dt = np.diff(ts_s); ok = dt > 0
    if not ok.any():
        return None
    r = np.diff(np.log(prices))[ok]; dt = dt[ok]
    age = now_s - ts_s[1:][ok]
    w = np.power(0.5, age / halflife_s)
    var = float(np.sum(w * r * r) / np.sum(w * dt))
    return math.sqrt(var) if var > 0 else None
```

### 4.5 Runner glue (on-demand evaluation; fast_player calls this on each spike)

```python
def _inputs(self, w, now):
    m = self.cfg.model
    sigma = self.sigma(now)                        # cached per second
    if sigma is None:
        return None
    lags = {int(k): float(v) for k, v in vars(m.lag_weights).items()}
    lo_ms = (w.end - max(lags) - m.twap_seconds) * 1000
    spot = {t // 1000: float(v) for t, v in self.state.spot_points.items() if t >= lo_ms and t % 1000 == 0}
    return sigma, lags, spot

def evaluate_v3(self, w, now):
    m = self.cfg.model
    inp = self._inputs(w, now)
    if inp is None:
        return None
    sigma, lags, spot = inp
    cl_ts, cl_price = self.state.spot_latest
    ex, until = self.state.exchange_series(now, self.cfg.feeds.stale_after_s, since_ms=cl_ts - 5000)
    if not ex:
        return None                                # no fresh exchange -> v3 outputs nothing
    nc = nowcast_spot(cl_ts // 1000, cl_price, ex, min(until, now + 500), m.exchange_offset_ms)
    if nc is None:
        return None
    P, est, until_s = nc
    S0 = float(w.ptb_ours)                         # our priceToBeat = TWAP point at exactly the window start
    p, mean, std, known = fair_value_settlement(S0, P, until_s, w.end, sigma, {**est, **spot}, lags,
                                                m.twap_seconds, m.dist, m.student_t_df)
    return {"secs_left": w.end - now / 1000, "p_up": p, "mean": mean, "std": std, "sigma": sigma,
            "twap_known": known, "spot": P, "s0": S0}
```

The runner also calls `strategy.on_tick(ctx)` every 0.5 s for every active window (fast_player uses this for exits). It sets `s.runner = self` on every strategy that has a `runner` attribute.

**Known weakness.** Model v3 is overconfident near 1. When it said 0.97, those entries won only 0.75-0.85. It also lost to the raw market mid out of sample at every horizon except the last 10 s. The edge here is in the *timing* of the spike, not in the model's accuracy.

---

## 5. Fees (`btc5m/fees.py`)

Taker fee = `shares × rate × p × (1 − p)`, with `rate = feeSchedule.rate` (0.07 for crypto). It is rounded to 5 decimals (half-up); anything below 0.00001 rounds to 0. Makers pay nothing. Refuse any `feeSchedule.exponent` other than 1.

```python
def taker_fee(shares, price, schedule, fees_enabled=True):
    if not fees_enabled:
        return 0.0
    _check(schedule)   # raises FeeScheduleUnsupported if no schedule or exponent != 1
    c, p, r = Decimal(str(shares)), Decimal(str(price)), Decimal(str(schedule["rate"]))
    return float((c * r * p * (1 - p)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))

def fee_per_share(price, schedule, fees_enabled=True):
    """Unrounded per-share fee, for edge calculations."""
    if not fees_enabled:
        return 0.0
    _check(schedule)
    return float(schedule["rate"]) * price * (1 - price)
```

---

## 6. Strategy base (`btc5m/strategies/base.py`)

```python
@dataclass
class TickCtx:
    window: object        # .slug .start .end .tick .min_size .fee_schedule .fees_enabled .ptb_ours .token(side)
    now_ms: int
    secs_left: float
    p_up: float
    books: dict           # token -> Book
    model_version: str = ""
    exchange_price: float | None = None
    spot: float | None = None
    s0: float | None = None
    sigma: float | None = None

    def book(self, side):      return self.books.get(self.window.token(side))
    def best_ask(self, side):  b = self.book(side); return b.best_ask() if b else None
    def best_bid(self, side):  b = self.book(side); return b.best_bid() if b else None
    def fair(self, side):      return self.p_up if side == "Up" else 1.0 - self.p_up
    def fee_ps(self, price):   return F.fee_per_share(price, self.window.fee_schedule, self.window.fees_enabled)

class Strategy:
    name = "base"
    uses_v3 = False
    def __init__(self, engine, params, cfg):
        self.engine, self.p, self.cfg = engine, params, cfg
        self.model_version = cfg.model.v3_version if self.uses_v3 else cfg.model.version
    def on_tick(self, ctx): raise NotImplementedError
```

Strategies only decide. The engine enforces every limit and simulates every fill.

---

## 7. The strategies (`btc5m/strategies/fast_player.py`, complete)

### 7.1 Rules in words

**Spike detection (on every Coinbase/Kraken tick, not on the 0.5 s timer).**
- composite = mean of the latest prices of the fresh exchanges (each updated within 3 s).
- Keep a 1 s history of the composite.
- move = composite now / composite ~1 s ago − 1.
- It is a spike if |move| ≥ 2 bps.
- History must cover the full lookback; otherwise there is no signal.

**Entry gates (per active window).**
- Our priceToBeat is known.
- At least 3 s left.
- Fewer than 2 entries in this window.
- At least 5 s since the last entry in this window.
- No open order of this strategy in this window.
- Model v3 has output.

**Side and edge.**
- Side = Up if move > 0, else Down.
- edge = v3 fair(side) − best ask − fee per share at the ask.
- Require edge ≥ 0.02. Otherwise the book has already repriced.

**Limit price.** Start at the ask. Step up one tick at a time while `fair − q − fee(q) ≥ 0.02` (and q < 1). The result is the highest price that still clears the minimum edge.

**Stake and size.**
- Stake = 4% of equity (conservative ledger: cash + reserved + positions at cost).
- size = stake / (limit + rate × 0.25), since 0.25 is the worst-case p(1−p) for the fee.
- The order is a taker BUY at `limit` for `size` shares.

**Exit (fast_player only, every 0.5 s on the runner tick).**
- When net holdings in a window are ≥ the minimum size, compute lock = 1 − (cost + fees)/shares of the held side − opposite ask − fee(opposite ask). This is the dollars per share you would lock by buying the other side now, because an Up+Down pair pays $1.
- Buy the opposite side for the same shares if lock ≥ 0.02, or if the position is ≥ 30 s old and lock ≥ −0.01.
- Otherwise hold to settlement.

**fast_player_hold.** No exits, held to settlement.

**fast_player_livesim.** Hold, but stake = max(4% of equity, just over 5 shares and ≥ $1). The engine treats it specially (§8.3).

### 7.2 Code

```python
from collections import deque
from .base import Strategy, TickCtx


class FastPlayer(Strategy):
    name = "fast_player"
    uses_v3 = True

    def __init__(self, engine, params, cfg):
        super().__init__(engine, params, cfg)
        self.runner = None                                   # set by Runner: on-demand model v3
        self.hist: deque[tuple[int, float]] = deque()        # (recv_ms, composite price)
        self.entries: dict[str, int] = {}                    # slug -> entries placed
        self.last_entry: dict[str, int] = {}                 # slug -> ms of the last entry
        self.entry_time: dict[tuple[str, str], int] = {}     # (slug, side) -> ms of the first entry
        engine.state.exchange_listeners.append(self.on_exchange)

    # --- entries: on every exchange tick ---
    def spike(self, recv: int) -> float | None:
        comp = self.engine.state.exchange_composite(recv, self.cfg.feeds.stale_after_s)
        if comp is None:
            return None
        self.hist.append((recv, comp))
        horizon = recv - self.p.lookback_ms
        while len(self.hist) > 1 and self.hist[1][0] <= horizon:
            self.hist.popleft()
        t0, ref = self.hist[0]
        if t0 > horizon:
            return None                                       # not enough history yet
        move = comp / ref - 1
        return move if abs(move) * 1e4 >= self.p.spike_bps else None

    def on_exchange(self, name: str, recv: int, price: float) -> None:
        move = self.spike(recv)
        if move is None or self.runner is None:
            return
        for w in self.runner.windows.active(recv / 1000):
            if w.ptb_ours is None or w.end - recv / 1000 < self.p.min_secs_left:
                continue
            if self.entries.get(w.slug, 0) >= self.p.max_entries_per_window:
                continue
            if recv - self.last_entry.get(w.slug, 0) < self.p.cooldown_s * 1000:
                continue
            if self.engine.open_orders(self.name, w.slug):
                continue
            out = self.runner.evaluate_v3(w, recv)
            if out is None:
                continue
            side = "Up" if move > 0 else "Down"
            ctx = TickCtx(window=w, now_ms=recv, secs_left=out["secs_left"], p_up=out["p_up"],
                          books=self.engine.state.books, model_version=self.model_version,
                          spot=out.get("spot"), s0=out.get("s0"), sigma=out.get("sigma"))
            self.enter(ctx, side, move)

    def enter(self, ctx: TickCtx, side: str, move: float) -> None:
        w = ctx.window
        ba = ctx.best_ask(side)
        if not ba:
            return
        fair, ask = ctx.fair(side), ba[0]
        cap = getattr(self.p, "max_price", 1.0)               # fast_player_value only; 1.0 = no cap
        if ask > cap + 1e-9:
            return
        edge = fair - ask - ctx.fee_ps(ask)
        if edge < self.p.min_edge:
            return                                            # book already repriced: nothing to catch
        limit, q = ask, round(ask + w.tick, 6)
        while q < 1 and q <= cap + 1e-9 and fair - q - ctx.fee_ps(q) >= self.p.min_edge:
            limit, q = q, round(q + w.tick, 6)
        if not self.allow(ctx):
            return
        rate = float(w.fee_schedule.get("rate", 0)) if w.fees_enabled else 0.0
        usd = self.stake_usd(ctx, fair, limit + ctx.fee_ps(limit))
        if usd <= 0:
            return
        size = usd / (limit + rate * 0.25)
        o = self.engine.submit(self.name, w, side, "taker", limit, size, model_p=fair, expected_edge=edge,
                               reason=f"spike {move * 1e4:+.2f}bps edge={edge:.3f}")
        if o is not None:
            self.entries[w.slug] = self.entries.get(w.slug, 0) + 1
            self.last_entry[w.slug] = ctx.now_ms
            self.entry_time.setdefault((w.slug, side), ctx.now_ms)

    def allow(self, ctx: TickCtx) -> bool:
        return True

    def stake_usd(self, ctx: TickCtx, fair: float, cost_per_share: float) -> float:
        return self.p.stake_frac * self.engine.ledger(self.name).equity()

    # --- exits: on the regular strategy tick ---
    def on_tick(self, ctx: TickCtx) -> None:
        w = ctx.window
        pos = self.engine.ledger(self.name).positions.get(w.slug)
        if not pos or self.engine.open_orders(self.name, w.slug):
            return
        up, dn = pos.get("Up"), pos.get("Down")
        net = (up.shares if up else 0.0) - (dn.shares if dn else 0.0)
        if abs(net) < w.min_size:
            return                                            # flat, or too small to trade out of
        held_side, opp = ("Up", "Down") if net > 0 else ("Down", "Up")
        held = pos[held_side]
        oa = ctx.best_ask(opp)
        if not oa:
            return
        lock = 1 - (held.cost + held.fees) / held.shares - oa[0] - ctx.fee_ps(oa[0])   # $ per share if locked now
        held_for = (ctx.now_ms - self.entry_time.get((w.slug, held_side), ctx.now_ms)) / 1000
        if lock >= self.p.take_profit or (held_for >= self.p.max_hold_s and lock >= -self.p.max_lock_loss):
            self.engine.submit(self.name, w, opp, "taker", oa[0], abs(net), model_p=ctx.fair(opp),
                               expected_edge=lock, reason=f"exit lock {lock:+.3f}/sh after {held_for:.0f}s")


class FastPlayerHold(FastPlayer):
    """Same entries as fast_player, held to settlement (no quick exit): one taker fee instead of two."""
    name = "fast_player_hold"

    def on_tick(self, ctx: TickCtx) -> None:
        return


class FastPlayerLiveSim(FastPlayerHold):
    """fast_player_hold run the way the live bot ran it, on paper. Stake: 4% of equity, but at least Polymarket's
    minimum order (min_size shares and $1). Engine: live-style fills, no risk limits, no 99c sell, own bankroll."""
    name = "fast_player_livesim"

    def stake_usd(self, ctx: TickCtx, fair: float, cost_per_share: float) -> float:
        w = ctx.window
        rate = float(w.fee_schedule.get("rate", 0)) if w.fees_enabled else 0.0
        limit = cost_per_share - ctx.fee_ps(cost_per_share)          # ~ the limit price (cost minus its fee)
        floor = max(w.min_size * 1.02 * (limit + rate * 0.25), 1.0)  # just over min_size shares and >= $1
        return max(super().stake_usd(ctx, fair, cost_per_share), floor)
```

(The same file also has `FastPlayerKelly` and `FastPlayerValue`, which never buys above `max_price = 0.60`. They are not part of this spec. `max_price` is the only reason for the `cap` lines in `enter`.)

### 7.3 Parameters (`config.toml`)

All three use the same values:

```toml
[strategies.fast_player]          # same block for fast_player_hold and fast_player_livesim
spike_bps = 2.0                   # composite move within lookback_ms
lookback_ms = 1000
min_edge = 0.02                   # v3 fair - ask - fee at entry
stake_frac = 0.04                 # per entry, of equity
max_entries_per_window = 2
cooldown_s = 5
min_secs_left = 3
take_profit = 0.02                # fast_player exits only
max_hold_s = 30                   # fast_player exits only
max_lock_loss = 0.01              # fast_player exits only

[feeds]
stale_after_s = 3.0
required = ["chainlink_spot", "chainlink_twap", "clob"]

[model]
v3_version = "v3-ex"
exchange_offset_ms = -250
sigma_halflife_s = 600
sigma_lookback_s = 1800
sigma_min_points = 300
twap_seconds = 60
tick_interval_s = 0.5
dist = "normal"
[model.lag_weights]
2 = 0.36
3 = 0.64

[paper]
bankroll = 100.0
latency_ms = 300
share_decimals = 2
[paper.bankrolls]
fast_player_livesim = 125.0
[paper.live_fills]
strategies = ["fast_player_livesim"]
match_delay_ms = 450
max_wait_ms = 3000

[risk]
max_stake_per_window_frac = 0.20
max_open_exposure_frac = 0.50
max_daily_loss_frac = 0.20
no_limits = ["fast_player_livesim"]

[sell99]                          # sells held shares once the best bid >= 0.99
enabled = true
price = 0.99
cooldown_s = 1.0
exempt = ["fast_player_livesim"]
```

---

## 8. Paper execution engine: what each strategy actually gets

Two ledgers per strategy, `conservative` and `optimistic`. For taker orders they are identical. Decisions use the conservative ledger.

### 8.1 Common checks in `submit` (all three)

```python
def submit(self, strategy, w, side, kind, price, size, *, model_p=None, expected_edge=None, reason=""):
    now = now_ms()
    block = self.block_reason()          # validation gate not reached / resolution mismatch / stale required feed
    if block: skip(...); return None
    size = round_down(size, 2)
    if size < w.min_size: skip("min_size"); return None          # 5 shares
    if not (0 < price < 1) or not on_tick(price, w.tick): skip("tick"); return None
    live = kind == "taker" and strategy in self.live_fill
    delay = self.match_delay_ms if live else self.latency_ms     # 450 vs 300 ms
    if now + delay >= w.end * 1000: skip("window_closed"); return None
    fee_rate = float(w.fee_schedule.get("rate", 0)) if w.fees_enabled else 0.0
    reserve = size * price + (size * fee_rate * 0.25 if kind == "taker" else 0.0)   # worst-case fee
    budget = None
    if live:      # like the live bot: spend price*size + fee at the limit, in cents, at least $1
        budget = max(round(size * (price + F.fee_per_share(price, w.fee_schedule, w.fees_enabled)), 2), 1.0)
        reserve = max(reserve, budget)
    for rule in ("conservative", "optimistic"):
        led = self.ledger(strategy, rule)
        if self.unlimited(strategy):                 # fast_player_livesim: only cash limits it
            if reserve > led.cash: skip("bankroll"); return None
            continue
        if led.halted(now): skip("risk_limit"); return None            # daily-loss halt until UTC midnight
        floor = led.day_start_equity - 0.20 * led.bankroll             # worst case: everything open goes to 0
        if led.cash - reserve < floor: skip("risk_limit"); return None
        eq = led.equity()
        if led.window_stake(w.slug) + reserve > 0.20 * eq: skip("risk_limit"); return None
        if led.exposure() + reserve > 0.50 * eq: skip("risk_limit"); return None
        if reserve > led.cash: skip("bankroll"); return None
    ...create Order(active_ts=now + delay, match_ts=now + delay if live else None, conn=self.clob_conn),
       reserve cash in both ledgers, write the orders row...
    if live:
        self.awaiting.append(o)
        loop.call_later((delay + self.match_wait_ms) / 1000, self._match_timeout, o)
    else:
        loop.call_later(self.latency_ms / 1000, self._activate, o)   # -> _execute_taker
    return o
```

`skip()` writes one `skipped` row per (strategy, window, reason, side). Log every blocked signal: it is how you find out why a strategy did not trade.

### 8.2 Standard taker fill (fast_player, fast_player_hold)

300 ms after placement, walk the asks of the local book **as it is then**, up to the limit. Partial fills are allowed and the rest is killed. The taker fee is charged per level.

```python
def _execute_taker(self, o):
    book = self.state.books.get(o.token)
    now = now_ms()
    if book is None or self.state.age_s("clob", now) > self.cfg.feeds.stale_after_s:
        self._close(o, "failed", "no_book"); return
    left = o.size
    for price, size in book.ask_levels(None):
        if price > o.price + EPS or left <= EPS:
            break
        q = round_down(min(left, size), 2)
        if q <= 0:
            continue
        fee = F.taker_fee(q, price, o.window.fee_schedule, o.window.fees_enabled)
        for rule in RULES:
            self._fill(o, rule, now, price, q, fee, 0.0, "taker")
        left -= q
    if o.filled["conservative"] <= EPS:   self._close(o, "failed", "no_liquidity_at_limit")
    elif o.remaining() > EPS:              self._close(o, "partial", "rest_killed")
    else:                                  self._close(o, "filled", o.reason)
```

**Known optimism.** In spikes the CLOB feed itself lags 0.3-1.8 s. So "the local book 300 ms later" is often the book from before the move, and these fills are too good. Re-filling fast_player_hold's entries on the logged book at +300/450/600 ms gave +7.6% / +3.9% / +1.5% per $ against +10.3% logged. §8.3 exists to fix this.

### 8.3 Live-style fill (fast_player_livesim only)

This copies the real bot's FAK market order:
- It spends a **dollar budget** (fee included) at prices up to the limit, so a better price buys more shares.
- It fills against Polymarket's book as it was at **Polymarket time** placed + 450 ms. The real bot's answers took 529 ms median and were matched about 80 ms before the answer.

The engine is registered as a CLOB `pre_listener`. On the first message *of that market* stamped after the match time, the book we hold right now is the book at that moment, however late the feed runs. This rule reproduced the real result (filled or not) for 28 of 30 live orders.

```python
def before_event(self, ev, src, conn=0):
    if conn != self.clob_conn:
        self.clob_conn, self.clob_src = conn, src
    elif src > self.clob_src:
        self.clob_src = src
    if not self.awaiting:
        return
    due = [o for o in self.awaiting if src > o.match_ts]
    if not due:
        return
    toks = ({ch.get("asset_id") for ch in ev.get("price_changes", ())} if "price_changes" in ev
            else {ev.get("asset_id")})
    for o in due:
        w = o.window
        if o.status in OPEN and not toks & {w.up_token, w.down_token}:
            continue                                  # wait for a message of this market
        self.awaiting.remove(o)
        if o.status in OPEN:
            self._execute_live(o)

def _execute_live(self, o):
    if o.conn != self.clob_conn:            # reconnected since placement: book at match time unknown
        self._close(o, "failed", "no_book"); return
    self._walk_live(o)

def _match_timeout(self, o):                # 450 + 3000 ms after placement
    if o not in self.awaiting:
        return
    self.awaiting.remove(o)
    if o.status not in OPEN:
        return
    if self.clob_src > o.match_ts and self.state.age_s("clob") <= self.cfg.feeds.stale_after_s:
        self._execute_live(o)               # stream is past the match time: market was just quiet
    else:
        self._close(o, "failed", "no_book")

def _walk_live(self, o):
    """FAK BUY spending at most o.budget (fee included) at prices <= o.price."""
    book = self.state.books.get(o.token)
    now = now_ms()
    if book is None:
        self._close(o, "failed", "no_book"); return
    w, dec = o.window, 2
    left = o.budget
    for price, size in book.ask_levels(None):
        if price > o.price + EPS or left < 0.01:
            break
        q = round_down(min(size, left / (price + F.fee_per_share(price, w.fee_schedule, w.fees_enabled))), dec)
        fee = F.taker_fee(q, price, w.fee_schedule, w.fees_enabled)
        while q > 0 and q * price + fee > left + EPS:            # fee rounding: never overspend
            q = round_down(q - 10 ** -dec, dec)
            fee = F.taker_fee(q, price, w.fee_schedule, w.fees_enabled)
        if q <= 0:
            break
        for rule in RULES:
            self._fill(o, rule, now, price, q, fee, 0.0, "taker")
        left -= q * price + fee
    if o.filled["conservative"] <= EPS:   self._close(o, "failed", "no_liquidity_at_limit")
    elif left > 0.02:                      self._close(o, "partial", "rest_killed")
    else:                                  self._close(o, "filled", o.reason)
```

### 8.4 The 99c sell rule (fast_player, fast_player_hold; not livesim)

On every book change and every runner tick, if a bot holds shares of a token whose best bid is ≥ 0.99, the engine sends a taker SELL limited at 0.99:
- Same 300 ms latency; it walks the bids down to 0.99, with the taker fee.
- It needs at least 5 shares held.
- 1 s cooldown per holding.

The position shrinks at average cost. It is optional; it was added mid-evaluation on 2026-10-21. If you want the purest "hold", leave it out.

### 8.5 Ledger and settlement

- **Equity** = cash + reserved + positions at cost.
- **Fill**: moves cash from the reservation into the position and pays the fee out of the reservation.
- **Order closed** (filled, failed or expired): the leftover reservation is released.
- **Settlement** on the first official result (Gamma `outcomePrices` `["1","0"]` / `["0","1"]`, about 80-140 s after the end; poll with a cache-busting param): winning shares pay $1, losing shares $0.
- **Window PnL** = payout − cost − fees.

```python
def settle(self, ts, slug, outcome):
    sides = self.positions.pop(slug, {})
    pnl = 0.0
    for side, pos in sides.items():
        if pos.shares <= EPS:
            continue
        if side == outcome:
            self.cash += pos.shares
            pnl += pos.shares - pos.cost - pos.fees
        else:
            pnl -= pos.cost + pos.fees
    return pnl
```

**Safety gates in the original project.** Trading starts only after 12 consecutive windows where our priceToBeat/finalPrice/outcome matched Gamma's (Step 0 validation). Any mismatch halts everything. Keep this: it proves you are computing priceToBeat correctly before you trust any result.

---

## 9. Why the live copy exists (lessons from real money)

On 2026-10-21, 05:24-09:54Z, the real bot (fast_player_hold logic on a ~$15 wallet) placed 31 orders:
- Only 20 of the 31 matched a paper decision. The live server's Kraken feed used `event_trigger: "bbo"`, so a stale Kraken price stayed "fresh" in its spike composite. The paper logger drops Kraken after 3 s without trades, which happens 26% of the time. **Use identical feed definitions in paper and live.**
- On the 20 shared decisions, paper filled 15 and live filled 6. The 300 ms paper rule is optimistic because the book feed lags in spikes.
- With a $10 wallet, 4% stakes were below Polymarket's minimum on 302/302 entries. With $125 or more, all of them are placed. That is why livesim has $125 and a minimum-order floor.

Results so far (paper, from the 2026-10-21 analysis, all provisional; the verdict needs 300 trades or 5 days):
- fast_player: +$33.70 over 123 windows, t = 2.5, consistent across both halves. It was the only significant strategy.
- fast_player_hold: +$76, t = 1.6.

Profit by entry price, fast_player_hold, per $1, held to settlement:

| Entry price | Return per $1 | Entries (n) |
| - | - | - |
| < 0.30 | +1.33 | 6 |
| 0.30-0.45 | +0.76 | 24 |
| 0.45-0.60 | +0.22 | 47 |
| 0.60-0.75 | +0.02 | 90 |
| 0.75-0.90 | −0.00 | 98 |
| ≥ 0.90 | −0.02 | 36 |

Re-filled 0.6-1 s late, only the entries below 0.60 stay clearly positive.

---

## 10. The real-money twin (reference only)

`livetrade/bot.py` drives the **unchanged** paper strategy class through an engine with the same interface (`submit`, `open_orders`, `ledger().equity()`, `skip`, `state`). The only subclass change is the stake floor, identical to livesim's:

```python
class LiveFastPlayerHold(FastPlayerHold):
    def stake_usd(self, ctx, fair, cost_per_share):
        w = ctx.window
        rate = float(w.fee_schedule.get("rate", 0)) if w.fees_enabled else 0.0
        limit = cost_per_share - ctx.fee_ps(cost_per_share)
        floor = max(w.min_size * 1.02 * (limit + rate * 0.25), 1.0)
        return max(super().stake_usd(ctx, fair, cost_per_share), floor)

class RunnerAdapter:          # what FastPlayer expects from Runner
    def __init__(self, bot): self.bot, self.windows = bot, self
    def active(self, t):
        w = self.bot.window()
        return [w] if w is not None and w.start <= t < w.end else []
    def evaluate_v3(self, w, now): return self.bot.runner.evaluate_v3(w, now)
```

**Order path.** The live engine's `submit` runs the same checks:
- min size, tick, at least 1 s left;
- cash = `price*size + fee*size`;
- the gate (clock > 250 ms off NTP, stale feeds, resolution mismatch).

Then it fires a FAK market BUY with `max_spend = max(round(cost, 2), 1.0)` and `max_price = limit` through the official `polymarket-client` SDK (`place_market_order(token_id, side="BUY", amount, max_spend, max_price, order_type="FAK")`). The fill (shares = `taking_amount`, cash = `making_amount`) is read from the answer, not from the user stream, so it is never counted twice. Measured: 344-530 ms click to answer.

**If you rebuild the live bot:**
- Keep the private key in its own gitignored file, read by that process only.
- Keep the trading SDK in a separate venv; it pins an older `websockets`.
- Test only with a fake client. Never place real orders to test.
- Auto-claiming winnings needs a Polymarket Builder API key.

---

## 11. Rebuild checklist

1. Feeds: CLOB book (with the `pre_listeners` hook), PolyBolt Chainlink spot+TWAP (use `full_accuracy_value`, check `source == "chainlink"`), Coinbase and Kraken trade tickers into `State.add_exchange`.
2. Window scheduler: Gamma prefetch and token subscription 30 s before the start. `ptb_ours` = the TWAP point at exactly the start. Poll results with a cache-busting param.
3. Validation: our priceToBeat/finalPrice/outcome vs Gamma for every window. Gate trading on 12 consecutive matches.
4. `fees.py`, `model.py` (§4), runner with `evaluate_v3` and a 0.5 s tick.
5. Engine: two ledgers, reservations, the 300 ms taker walk, live-style fills, risk limits with a `no_limits` list, per-strategy bankrolls, settlement, a `skipped` log.
6. Strategies from §7.2 with the §7.3 config.
7. Tests to write first:
   - fee table: 100 shares at $0.01-$0.99 vs the docs' Crypto table;
   - the spike needs a full lookback;
   - the limit-walk stops at min_edge;
   - livesim stake ≥ 5 shares and ≥ $1;
   - the live walk never overspends its budget;
   - a reconnect between placement and match gives `no_book`;
   - a taker fill uses the book at activation, not at placement.
8. Keep the PC's clock NTP-synced (warn above 50 ms). Latency maths depends on it.

"""Fast player (strategy "fast_player"): a latency taker on BTC Up/Down windows. Rebuilt 2026-10-22 on this
engine from the user's earlier btc5m bots (FAST_PLAYER_SPEC.md in the project root), for the Dublin server.

Edge: when BTC jumps on the exchanges, Chainlink (which settles the window) follows ~1.3 s later and
Polymarket's book reprices over ~0.5-2 s. Measured from Dublin (26 min, 2026-10-22): 50 ms after a spike the
spike-side ask had moved in ~10% of cases, after 500 ms in ~60%; bought at the ask within 100 ms and marked
at the bid 5 s later, a spike made ~+9c/share after fees (+3c when bought 1 s late).

Signal, on every exchange tick (not on a timer):
  * per exchange in `sources` (coinbase trade price, kraken trade price, binance spot / binance_fut futures best
    bid/ask mid; binance_fut = futures last trade) the move
    against its own price `lookback_ms` ago; an exchange silent for `feed_stale_ms` is left out. Moves are
    averaged, not prices, so the basis between venues (USD vs USDT) never looks like a spike;
  * spike = |mean move| >= spike_bps (combine="max": the largest move, if no exchange disagrees).
Entry, per active window, side = the spike's direction:
  * fair value (`fair_mode`): "anchored" = the Up book mid before the spike + the change the price model
    predicts for this move (the market stays the anchor; the model only says how far this move should push
    it, less near the end of a window); "model" = the pure model; "both" = the lower of the two;
  * edge = fair - cost per share (the taker fee is taken in shares) >= min_edge; the limit is walked up a tick
    at a time while that still holds, never outside min_price..max_price;
  * a FAK buy spending `stake_frac` of the portfolio (at least Polymarket's minimum: min_size shares, $1),
    inside per-window / open-exposure / daily-loss limits.
Exit (`exit_mode`): "hold" to settlement, or "scalp": sell into the bid once that pays take_profit per share
  after fees, or once the position is exit_after_s old and the loss is at most max_exit_loss; else hold.

Paper fills copy a real FAK order (the old bot's "local book 300 ms later" fills were ~2.5x too good, because
the book feed itself lags in spikes): the order reaches the matching engine `latency_ms` later in Polymarket's
clock and fills against the book as it stood then - the book we hold when the first message stamped after that
moment arrives, before that message is applied. A quiet market fills after `match_wait_ms` against the current
book; a book reconnect in between fails the order.

Feeds, books, the price model, settlement on the Chainlink TWAP, persistence and the UI snapshot are the PaperBot
engine (market_maker.py); this module adds the Kraken and Binance feeds, the signal, FAK orders and sells.
"""
import asyncio
import collections
import dataclasses
import json
import math
import time
from datetime import datetime

import orjson

from ...config import BINANCE_FUT_WS, KRAKEN_WS
from .market_maker import Order, PaperBot

SOURCES = ("coinbase", "kraken", "binance", "binance_fut")
KR_SYMBOL = "BTC/USD"
CL_STALE_MS = 5000          # Chainlink ticks arrive ~1.3 s after their timestamp, one per second
EATEN_MS = 3000             # liquidity our paper takers used stays used this long (the real feed never removes it)


@dataclasses.dataclass
class FastConfig:
    bankroll: float = 500.0
    windows: tuple = (5,)
    # signal
    sources: tuple = ("coinbase", "kraken", "binance")   # + binance_fut (USD-M futures bookTicker)
    spike_bps: float = 2.0
    lookback_ms: float = 1000.0
    combine: str = "mean"             # mean | max
    feed_stale_ms: float = 3000.0
    # entry
    fair_mode: str = "anchored"       # anchored | model | both
    min_edge: float = 0.02
    min_price: float = 0.05
    max_price: float = 0.85
    start_delay_s: float = 2.0        # our price to beat is ready ~1.5 s after the open
    stop_before_end_s: float = 3.0
    max_entries_per_window: int = 2
    cooldown_s: float = 5.0
    stake_frac: float = 0.04
    max_stake_usd: float = 100.0
    # exit
    exit_mode: str = "hold"           # hold | scalp
    take_profit: float = 0.04
    exit_after_s: float = 10.0
    max_exit_loss: float = 0.02
    # risk
    max_window_frac: float = 0.20
    max_open_frac: float = 0.50
    max_daily_loss_frac: float = 0.20
    # model / simulation
    spot_source: str = "coinbase"     # spot level for the model: coinbase | binance | blend (adjusted to Chainlink)
    vol_floor: float = 0.4
    latency_ms: float = 50.0          # order -> matching engine (Dublin: ~44 ms round trip)
    match_wait_ms: float = 1500.0
    fee_rate: float = 0.07            # used only when the market lists none
    rebate_share: float = 0.15        # (engine field; takers earn no rebates)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in dataclasses.fields(cls)}
        d = {k: (tuple(v) if isinstance(v, list) else v) for k, v in (d or {}).items() if k in names}
        return cls(**d)

    def to_dict(self):
        return dataclasses.asdict(self)


class Fak:
    """A fill-and-kill order on its way to the matching engine. Buys spend `budget` $, sells `shares`."""
    __slots__ = ("m", "side", "buy", "limit", "budget", "shares", "placed", "match_ts", "sig")

    def __init__(self, m, side, buy, limit, budget, shares, placed, match_ts, sig):
        self.m, self.side, self.buy, self.limit = m, side, buy, limit
        self.budget, self.shares, self.placed, self.match_ts, self.sig = budget, shares, placed, match_ts, sig


def _st(m):
    """Per-window strategy state, kept on the Market."""
    st = m.__dict__.get("fp")
    if st is None:
        st = m.fp = {"entries": 0, "last": 0.0, "entry_ms": {}, "mids": collections.deque(), "fair_ms": 0.0,
                     "basis": {}, "eaten": {}}     # basis: $ cost of the shares held per side; eaten: see _avail
    return st


class FastBot(PaperBot):
    CONFIG = FastConfig
    STRATEGY = "fast_player"

    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self.ex = {s: collections.deque() for s in SOURCES}     # (recv_ms, price) per exchange
        self._kr_last = None
        self.spike = None                                       # the spike in progress
        self._spike_n = 0
        self.pending = []                                       # Fak orders not matched yet
        self.signals = collections.deque(maxlen=300)            # one row per spike x window (+ exits)
        self.status["kraken"] = "stopped"
        self.status["binance_fut"] = "stopped"
        self.lat["signal_us"] = collections.deque(maxlen=20000)

    def reset(self, bankroll=None):
        super().reset(bankroll)
        self.pending = []
        self.signals.clear()

    # ------------------------------------------------------------------ feeds
    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        src = set(self.cfg.sources)
        feeds = [self._coinbase(), self._chainlink()]
        if "binance" in src or self.cfg.spot_source != "coinbase":
            feeds.append(self._binance())
        if "kraken" in src:
            feeds.append(self._kraken())
        if "binance_fut" in src:
            feeds.append(self._binance_fut())
        tasks = [asyncio.create_task(c) for c in (*feeds, self._scheduler(), self._ticker(), self._clock(),
                                                  self._resolver(), self._official_prices())]
        await self._stop.wait()
        for t in tasks + list(self._ws_tasks.values()):
            t.cancel()
        self._ws_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _kraken(self):
        import websockets
        backoff = 1
        while True:
            try:
                self.status["kraken"] = "connecting"
                async with websockets.connect(KRAKEN_WS, ping_interval=15, ping_timeout=15, max_size=None,
                                              max_queue=None, open_timeout=10, compression=None) as ws:
                    # default event_trigger = trades: a message per trade, so a quiet Kraken goes stale
                    await ws.send(json.dumps({"method": "subscribe",
                                              "params": {"channel": "ticker", "symbol": [KR_SYMBOL]}}))
                    self.status["kraken"] = "connected"
                    backoff = 1
                    while True:
                        self._on_kraken_msg(orjson.loads(await asyncio.wait_for(ws.recv(), timeout=20)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status["kraken"] = "reconnecting"
                self._err(f"kraken: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20)

    async def _binance_fut(self):
        import websockets
        backoff = 1
        while True:
            try:
                self.status["binance_fut"] = "connecting"
                async with websockets.connect(BINANCE_FUT_WS, ping_interval=15, ping_timeout=15, max_size=None,
                                              max_queue=None, open_timeout=10, compression=None) as ws:
                    self.status["binance_fut"] = "connected"
                    backoff = 1
                    while True:
                        self._on_binance_fut_msg(orjson.loads(await asyncio.wait_for(ws.recv(), timeout=20)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status["binance_fut"] = "reconnecting"
                self._err(f"binance_fut: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20)

    def _on_binance_fut_msg(self, d):
        """Futures aggTrade (live: last trade price `p`) or bookTicker (older recordings: mid of `b`/`a`)."""
        try:
            mid = float(d["p"]) if "p" in d else (float(d["b"]) + float(d["a"])) / 2
        except (KeyError, TypeError, ValueError):
            return
        self.counts["binance_fut"] += 1
        self._on_ex("binance_fut", mid, self.clock() * 1000.0)

    def _on_kraken_msg(self, d):
        if d.get("channel") != "ticker":
            return
        recv = self.clock() * 1000.0
        for x in d.get("data") or ():
            if x.get("symbol") != KR_SYMBOL or x.get("last") is None:
                continue
            key = (x["last"], x.get("volume"))
            if key == self._kr_last:          # a best bid/ask update (recordings use event_trigger bbo): no trade
                continue
            self._kr_last = key
            self.counts["kraken"] += 1
            self._on_ex("kraken", float(x["last"]), recv)

    def _on_coinbase_msg(self, d):
        if d.get("type") != "ticker":
            return
        recv = self.clock() * 1000.0
        try:
            px, bid, ask = float(d["price"]), float(d["best_bid"]), float(d["best_ask"])
        except (KeyError, TypeError, ValueError):
            return
        try:
            ts = datetime.fromisoformat(d["time"].replace("Z", "+00:00")).timestamp() * 1000.0
            if self.clock_offset_ms is not None:
                self.lat["coinbase"].append(self._corr(recv) - ts)
        except Exception:
            ts = recv
        mid = (bid + ask) / 2
        self.cb.update(mid=mid, bid=bid, ask=ask, ts_ms=ts, recv_ms=recv)
        self.cb_hist.append((recv, mid))
        self.counts["coinbase"] += 1
        self._on_ex("coinbase", px, recv)

    def _on_binance_msg(self, d):
        try:
            mid = (float(d["b"]) + float(d["a"])) / 2
        except (KeyError, TypeError, ValueError):
            return
        recv = self.clock() * 1000.0
        self.bn.update(mid=mid, recv_ms=recv)
        self.bn_hist.append((recv, mid))
        self.counts["binance"] += 1
        self._on_ex("binance", mid, recv)

    # ------------------------------------------------------------------ signal
    def _on_ex(self, src, price, recv):
        h = self.ex[src]
        changed = not h or h[-1][1] != price
        h.append((recv, price))
        if not changed or src not in self.cfg.sources:
            return
        t0 = time.perf_counter()
        mv = self._move(recv)
        if mv is None or abs(mv) * 1e4 < self.cfg.spike_bps:
            self.spike = None
            return
        sp = self.spike
        if sp is None or (sp["move"] > 0) != (mv > 0):
            self._spike_n += 1
            sp = self.spike = {"id": self._spike_n, "t": recv, "move": mv, "seen": set()}
            self.counts["spikes"] += 1
        sp["move"] = mv
        for m in list(self.markets.values()):
            self._try_enter(m, mv, recv)
        self.lat["signal_us"].append((time.perf_counter() - t0) * 1e6)

    def _move(self, now):
        """Mean (or largest) move of the fresh exchanges over the lookback; None until history covers it."""
        cfg = self.cfg
        horizon = now - cfg.lookback_ms
        moves = []
        for s in cfg.sources:
            h = self.ex.get(s)
            if not h or now - h[-1][0] > cfg.feed_stale_ms:
                continue
            while len(h) > 1 and h[1][0] <= horizon:
                h.popleft()
            if h[0][0] > horizon:
                continue
            moves.append(h[-1][1] / h[0][1] - 1.0)
        if not moves:
            return None
        if cfg.combine == "max":
            big = max(moves, key=abs)
            return big if all(x * big >= 0 for x in moves) else 0.0
        return sum(moves) / len(moves)

    def _fee_ps(self, m, px):
        rate = m.fee_rate if m.fee_rate is not None else self.cfg.fee_rate
        return rate * px * (1 - px)

    def _cost_ps(self, m, px):
        """$ per share received on a taker buy at px (the fee is taken in shares)."""
        rate = m.fee_rate if m.fee_rate is not None else self.cfg.fee_rate
        return px / (1 - rate * (1 - px))

    def _mid_at(self, m, t):
        for ts, v in reversed(_st(m)["mids"]):
            if ts <= t:
                return v
        return m.mid("up")

    def _try_enter(self, m, mv, now):
        cfg = self.cfg
        now_s = now / 1000.0
        if m.minutes not in cfg.windows or not m.start + cfg.start_delay_s <= now_s <= m.end - cfg.stop_before_end_s:
            return
        sp = self.spike
        first = m.slug not in sp["seen"]
        sp["seen"].add(m.slug)
        side = "up" if mv > 0 else "down"
        sig = {"ts": now_s, "slug": m.slug, "win": m.minutes, "t_left": round(m.end - now_s, 1), "side": side,
               "move": round(mv * 1e4, 2), "spike": sp["id"], "ask": None, "fair": None, "edge": None,
               "limit": None, "usd": None, "result": None, "filled": None, "avg": None}
        why = self._entry(m, side, mv, now, sig)
        sig["result"] = why or "order"
        if why is None or first:
            if first:
                self.counts[f"sig_{sig['result']}"] += 1
            self.signals.append(sig)
        elif (sig["edge"] is not None and self.signals and self.signals[-1].get("spike") == sp["id"]
              and self.signals[-1]["slug"] == m.slug and self.signals[-1]["limit"] is None):
            self.signals[-1].update({k: sig[k] for k in ("ask", "fair", "edge", "result")})   # latest look at it

    def _entry(self, m, side, mv, now, sig):
        """Place the entry order; returns None, or why not."""
        cfg = self.cfg
        now_s = now / 1000.0
        st = _st(m)
        if any(o.m is m for o in self.pending):
            return "busy"
        if st["entries"] >= cfg.max_entries_per_window:
            return "max_entries"
        if now - st["last"] < cfg.cooldown_s * 1000:
            return "cooldown"
        if not self.cl or self._corr(self.clock() * 1000.0) - self.cl[-1][0] > CL_STALE_MS:
            return "stale_chainlink"
        if not m.book_ok or now - m.last_book_ms > cfg.feed_stale_ms:
            return "stale_book"
        if m.strike is not None and m.strike_official is not None and abs(m.strike - m.strike_official) > 1.0:
            return "strike_mismatch"
        eq = self.portfolio_value()
        if self.day_start_value is not None and eq < self.day_start_value - cfg.max_daily_loss_frac * cfg.bankroll:
            return "daily_loss"
        p_now = self._model(m, now_s)
        if p_now is None:
            return "no_model"
        fair_up = p_now
        if cfg.fair_mode in ("anchored", "both"):
            S = self._spot()
            mid0 = self._mid_at(m, now - cfg.lookback_ms)
            p_ref = self._model(m, now_s, S=S / (1 + mv)) if S else None
            if mid0 is None or p_ref is None:
                return "no_model"
            anch = min(max(mid0 + p_now - p_ref, 0.0), 1.0)
            fair_up = anch if cfg.fair_mode == "anchored" else (min(anch, p_now) if side == "up" else max(anch, p_now))
        fair = fair_up if side == "up" else 1.0 - fair_up
        m.fair = p_now
        ask = m.best_ask(side)
        sig["fair"] = round(fair, 4)
        if ask is None:
            return "no_ask"
        sig["ask"] = ask
        edge = fair - self._cost_ps(m, ask)
        sig["edge"] = round(edge, 4)
        if ask > cfg.max_price + 1e-9:
            return "price_cap"
        if ask < cfg.min_price - 1e-9:
            return "price_floor"
        if edge < cfg.min_edge:
            return "no_edge"
        limit, q = ask, round(ask + m.tick, 6)
        while q < 1 and q <= cfg.max_price + 1e-9 and fair - self._cost_ps(m, q) >= cfg.min_edge:
            limit, q = q, round(q + m.tick, 6)
        floor = max(m.min_size * limit * 1.02, 1.0)                 # Polymarket's minimum order
        usd = max(min(cfg.stake_frac * eq, cfg.max_stake_usd), floor)
        open_cost = sum(mk.total_cost() for mk in self.markets.values() if mk.state != "settled")
        usd = min(usd, cfg.max_window_frac * eq - m.total_cost(), cfg.max_open_frac * eq - open_cost,
                  self.cash - self._reserved())
        if usd < floor:
            return "risk_limit" if self.cash - self._reserved() >= floor else "cash"
        usd = math.floor(usd * 100) / 100
        sig["limit"], sig["usd"] = limit, usd
        self.pending.append(Fak(m, side, True, limit, usd, 0.0, now, self._corr(now) + cfg.latency_ms, sig))
        st["entries"] += 1
        st["last"] = now
        self.counts["orders"] += 1
        return None

    # ------------------------------------------------------------------ exits
    def _requote(self, m, now, force=False):
        """No quoting: window state, the model for the UI, and scalp exits."""
        cfg = self.cfg
        now_s = now / 1000.0
        if now_s >= m.end:
            if m.state in ("pending", "quoting"):
                m.state = "closed"
            return
        if m.state == "pending" and now_s >= m.start:
            m.state = "quoting"
        st = _st(m)
        if now - st["fair_ms"] >= 250:
            st["fair_ms"] = now
            m.fair = self._model(m, now_s)
        if cfg.exit_mode != "scalp" or now_s > m.end - 1.0 or any(o.m is m for o in self.pending):
            return
        for side in ("up", "down"):
            sh = m.shares[side]
            if sh < m.min_size:
                continue
            bid = m.best_bid(side)
            if bid is None:
                continue
            basis = st["basis"].get(side, max(m.cost[side], 0.0))     # restored after a restart: window cost
            gain = bid - self._fee_ps(m, bid) - basis / sh
            age = (now - st["entry_ms"].get(side, now)) / 1000.0
            if gain >= cfg.take_profit or (age >= cfg.exit_after_s and gain >= -cfg.max_exit_loss):
                sig = {"ts": now_s, "slug": m.slug, "win": m.minutes, "t_left": round(m.end - now_s, 1),
                       "side": side, "move": None, "spike": None, "ask": None, "fair": None, "edge": round(gain, 4),
                       "limit": bid, "usd": None, "result": "exit", "filled": None, "avg": None}
                self.signals.append(sig)
                self.pending.append(Fak(m, side, False, bid, 0.0, math.floor(sh * 100) / 100, now,
                                        self._corr(now) + cfg.latency_ms, sig))
                self.counts["exits"] += 1
                return

    # ------------------------------------------------------------------ execution
    def _on_book_msg(self, m, d):
        if self.pending:
            ts = None
            for e in d if isinstance(d, list) else (d,):
                if isinstance(e, dict) and e.get("timestamp"):
                    ts = max(ts or 0.0, float(e["timestamp"]))
            if ts is not None:
                for o in [o for o in self.pending if o.m is m and ts > o.match_ts]:
                    self._execute(o, self.clock() * 1000.0)       # the book as it stood at the match time
        super()._on_book_msg(m, d)
        mid = m.mid("up")
        mids = _st(m)["mids"]
        if mid is not None and (not mids or mids[-1][1] != mid):
            now = self.clock() * 1000.0
            mids.append((now, mid))
            while mids and mids[0][0] < now - 10_000:
                mids.popleft()

    def _tick(self, now):
        super()._tick(now)
        cfg = self.cfg
        for o in [o for o in self.pending if now >= o.placed + cfg.latency_ms + cfg.match_wait_ms]:
            if o.m.book_ok and now - o.m.last_book_ms < cfg.feed_stale_ms:
                self._execute(o, now)                             # market was quiet: the book is current
            else:
                self._close(o, "no_book")

    def _close(self, o, result):
        if o in self.pending:
            self.pending.remove(o)
        o.sig["result"] = result if o.buy else ("exit" if result == "filled" else f"exit_{result}")
        self.counts[f"{'buy' if o.buy else 'sell'}_{result}"] += 1

    def _avail(self, m, side, book, px, sz, now):
        """Shares at a level minus what our own earlier paper takers took from it (the feed never shows them)."""
        e = _st(m)["eaten"].get((side, book, px))
        return sz if e is None or now > e[1] else max(0.0, sz - e[0])

    def _eat(self, m, side, book, px, q, now):
        ea = _st(m)["eaten"]
        e = ea.get((side, book, px))
        ea[(side, book, px)] = [(e[0] if e and now <= e[1] else 0.0) + q, now + EATEN_MS]

    def _execute(self, o, now):
        m = o.m
        if not m.book_ok:                                         # reconnected: the book at match time is unknown
            self._close(o, "no_book")
            return
        if o.buy:
            left, got, spent = o.budget, 0.0, 0.0
            basis = _st(m)["basis"]
            for px, sz in sorted(m.books[o.side]["asks"].items()):
                if px > o.limit + 1e-9 or left < 0.01:
                    break
                q = math.floor(min(self._avail(m, o.side, "asks", px, sz, now), left / px) * 100) / 100
                if q <= 0:
                    continue
                self._eat(m, o.side, "asks", px, q, now)
                basis[o.side] = basis.get(o.side, 0.0) + q * px
                self._fill(m, Order(0, o.side, px, q, 0.0, now, 0), q, px, now, maker=False)
                left -= q * px
                got += q
                spent += q * px
            if got > 0:
                _st(m)["entry_ms"].setdefault(o.side, now)
                o.sig["filled"], o.sig["avg"] = round(got, 2), round(spent / got, 4)
            self._close(o, "filled" if left < 0.02 else "partial" if got > 0 else "missed")
        else:
            left, got, gross = o.shares, 0.0, 0.0
            for px, sz in sorted(m.books[o.side]["bids"].items(), reverse=True):
                if px < o.limit - 1e-9 or left < 0.01:
                    break
                q = math.floor(min(self._avail(m, o.side, "bids", px, sz, now), left, m.shares[o.side]) * 100) / 100
                if q <= 0:
                    continue
                self._eat(m, o.side, "bids", px, q, now)
                self._sell_fill(m, o.side, q, px, now)
                left -= q
                got += q
                gross += q * px
            if got > 0:
                o.sig["filled"], o.sig["avg"] = round(got, 2), round(gross / got, 4)
            if m.shares[o.side] < m.min_size:
                _st(m)["entry_ms"].pop(o.side, None)
            self._close(o, "filled" if left < 0.01 else "partial" if got > 0 else "missed")

    def _sell_fill(self, m, side, q, px, now):
        """A taker sell: the fee is paid in USDC. Window cost drops by the net proceeds, so the window's PnL
        (shares of the winner - cost) and the cash stay right."""
        fee = q * self._fee_ps(m, px)
        net = q * px - fee
        basis = _st(m)["basis"]
        if m.shares[side] > 0:
            basis[side] = basis.get(side, max(m.cost[side], 0.0)) * max(0.0, 1 - q / m.shares[side])
        m.shares[side] -= q
        m.cost[side] -= net
        m.fees_paid += fee
        m.fills += 1
        self.cash += net
        t = self.totals
        t["volume"] += q * px
        t["fills"] += 1
        t["taker_volume"] += q * px
        t["taker_fills"] += 1
        t["fees_paid"] += fee
        rec = {"ts": now / 1000.0, "slug": m.slug, "win": m.minutes, "side": side, "price": round(px, 4),
               "shares": round(-q, 6), "usd": round(-net, 6), "maker": False, "rebate": 0.0, "fee": round(fee, 6),
               "t_in": round(now / 1000.0 - m.start, 2),
               "fair": None if m.fair is None else round(m.fair if side == "up" else 1 - m.fair, 4),
               "btc": self.cb["mid"], "markout": None}
        self.fills.append(rec)
        self._wq.put(("fill", rec))

    def _reserved(self):
        return sum(o.budget for o in self.pending if o.buy)

    # ------------------------------------------------------------------ UI
    def _build_snapshot(self):
        super()._build_snapshot()
        s = self._snapshot
        now = time.time()
        s["signals"] = list(self.signals)[-150:]
        s["spike"] = None if self.spike is None else {"id": self.spike["id"], "move": self.spike["move"] * 1e4}
        s["exchanges"] = {k: (h[-1][1] if h else None, (now * 1000 - h[-1][0]) if h else None)
                          for k, h in self.ex.items()}
        s["orders"] = [{"slug": o.m.slug, "win": o.m.minutes, "start": o.m.start, "end": o.m.end, "side": o.side,
                        "price": o.limit, "size": o.budget / o.limit if o.buy else o.shares,
                        "remaining": o.budget / o.limit if o.buy else o.shares, "queue": 0,
                        "age": now - o.placed / 1000.0, "status": "sending"} for o in self.pending]

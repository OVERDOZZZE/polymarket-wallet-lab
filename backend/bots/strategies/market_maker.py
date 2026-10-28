"""Paper market-making bot for Polymarket's BTC Up/Down windows - a reconstruction of the
CrouchPredictions strategy (two-sided resting bids, hold to settlement, earn maker rebates).

Everything runs on one asyncio loop in a dedicated thread, so a price tick goes straight from
the socket to the quoting decision with no hand-off. Feeds (all public, no keys):
  * Coinbase Exchange websocket, BTC-USD ticker - the fastest spot price; drives the model.
  * Polymarket RTDS websocket, Chainlink btc/usd 1 s ticks - the markets settle on their 60 s TWAP
    (Chainlink's btc-usd-twap-60s stream, computed here with markets.twap_of).
    Also used to learn the Coinbase->Chainlink basis.
  * Polymarket CLOB market websocket - one connection per window (current + next, 5m and
    15m), opened before the window starts so there is never a resubscribe gap.

Paper fills are simulated from the real trade prints with price-time priority:
  * the Up and Down books are mirrors (Up bid p == Down ask 1-p), so a resting Up bid at p is
    hit by a taker SELL of Up at <= p, or a taker BUY of Down at >= 1-p;
  * at an existing price level we join the back of the queue: the shares already shown at
    that level must trade (or be cancelled) before we fill;
  * the queue ahead shrinks only by trades (the prints) and by cancels. A level shrink in the book is
    either a trade or cancels, and its print can arrive before or after it (22% of trades arrive after
    the shrink, measured 2026-10-21), so shrinks and prints are matched for LEVEL_MATCH_MS; a shrink
    left unmatched is cancels, spread pro rata over the level (only the part ahead of us moves us up);
  * a bid priced better than the print fills first;
  * orders only become live `latency_ms` after placement, and stay fillable for `latency_ms`
    after a cancel - the same delay a real order would see (200 ms: measured order round trips from
    this PC are 150-310 ms).
  Known limit: the feed sends one print per taker order with a single price, so an 11% minority of
  prints that sweep 2-3 levels are seen at one level.

Rebates are estimated with Polymarket's formula: fee-equivalent = shares * rate * p * (1-p),
times `rebate_share` (0.15 was calibrated from CrouchPredictions' actual MAKER_REBATE payouts;
the market's nominal rebateRate is 0.20 but the pool is shared).

Strategy id: "market_maker". Runs as its own process via backend/bots/runner.py (one process per bot).
Persistence: fills / windows / equity -> DB (bot_fills, bot_windows, bot_equity) tagged with bot id and
account number; live state -> data/bots/<id>/state.json; commands <- data/bots/<id>/control.json.
"""
import asyncio
import collections
import dataclasses
import json
import math
import os
import queue
import threading
import time
from datetime import datetime

import orjson
import requests

from ... import db
from ...config import (BINANCE_WS, BOTS_DIR, CLOB_MARKET_WS as CLOB_WS, COINBASE_WS, GAMMA,
                       RTDS_WS as RTDS_URL)
from ...polymarket.markets import TWAP_END_LAG, fetch_window_prices, twap_of, twap_secs
from ...util import ntp_offset_ms, write_text_atomic

CL_SYMBOL = "btc/usd"
PREOPEN_S = 20          # open the next window's book socket this early
MARKOUT_S = 10          # adverse-selection check horizon
LEVEL_MATCH_MS = 400    # a level shrink and the trade print behind it arrive within this of each other
ERR_REPEAT_S = 30       # the same error is written to bot.log at most once per this many seconds
LAT_STATS_N = 4000      # latency percentiles for the UI: over this many most recent samples per stage
WS_CLOSE_S = 1          # feed sockets: wait this long for the close handshake (Binance never answers; the 10 s
                        # default made a UI Stop overrun control.stop, get SIGKILLed and systemd restart the bot)
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Mozilla/5.0 (pmtool bot)"


@dataclasses.dataclass
class BotConfig:
    bankroll: float = 1000.0          # paper starting balance (USDC)
    windows: tuple = (5, 15)          # window lengths to trade (minutes)
    levels: int = 3                   # resting bids per side
    sizes: tuple = (5, 10, 20)        # shares per level, top level first
    base_edge: float = 0.01           # top bid sits this far below fair (pair cost ~ 1 - 2*edge)
    level_step: float = 0.01          # gap between ladder levels
    model_weight: float = 0.2         # fair = book mid + Coinbase lead + w * (model - mid)
    lead_weight: float = 1.0          # how much of the Coinbase-predicted Chainlink move to price in
    join_only: bool = True            # never bid above the current best bid
    skew_per_100: float = 0.01        # lower the heavy side's bids by this per 100 shares of imbalance
    max_side_shares: float = 600.0    # stop bidding a side above this many shares in one window
    max_imbalance: float = 150.0      # stop bidding the heavier side once it leads by this many shares
    max_window_cost: float = 250.0    # $ per window; above it only the lighter side is bid
    min_price: float = 0.03
    max_price: float = 0.97
    start_delay_s: float = 4.0        # CrouchPredictions' first fill is ~6 s after open
    stop_before_end_s: float = 3.0
    jump_bps: float = 4.0             # Coinbase move within 1 s that pulls all quotes
    jump_cooldown_ms: float = 1500.0
    latency_ms: float = 200.0         # simulated order/cancel travel time (measured round trips 150-310 ms)
    min_requote_ms: float = 25.0
    fee_rate: float = 0.07
    rebate_share: float = 0.15
    taker_enabled: bool = False       # also take when Coinbase says the ask is stale
    taker_edge: float = 0.04          # required edge after fees, per share
    taker_size: float = 10.0
    taker_cooldown_ms: float = 1000.0
    vol_floor: float = 0.4            # bps per sqrt(second), lower bound for the model
    spot_source: str = "coinbase"     # spot that drives the model: coinbase | binance | blend (both, basis-adjusted)
    maker_min_edge: float = -1.0      # rest a bid only if price <= model fair - this (any strategy); < -0.5 = off
    taker_fair: str = "blend"         # takes are judged on: blend (fair used for quoting) | model (pure model)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in dataclasses.fields(cls)}
        d = {k: (tuple(v) if isinstance(v, list) else v) for k, v in (d or {}).items() if k in names}
        return cls(**d)

    def to_dict(self):
        return dataclasses.asdict(self)


def _jsonable(o):
    """orjson fallback for the UI snapshot."""
    return list(o) if isinstance(o, (collections.deque, set, frozenset)) else str(o)


def _ncdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _floor_tick(p, tick):
    return math.floor(p / tick + 1e-9) * tick


def _pct(seq, q):
    if not seq:
        return None
    s = sorted(seq)
    return s[min(len(s) - 1, int(q * len(s)))]


# --------------------------------------------------------------------------
class Order:
    __slots__ = ("id", "side", "price", "size", "remaining", "queue", "placed_ms", "active_ms", "cancel_ms",
                 "taker")

    def __init__(self, oid, side, price, size, queue_ahead, now, latency, taker=False):
        self.id, self.side, self.price, self.size, self.remaining = oid, side, price, size, size
        self.queue = queue_ahead
        self.placed_ms, self.active_ms, self.cancel_ms = now, now + latency, None
        self.taker = taker

    def live(self, now, latency):
        return now >= self.active_ms and (self.cancel_ms is None or now < self.cancel_ms + latency)


class Market:
    """One Up/Down window: its books, our resting orders, inventory and settlement."""

    def __init__(self, slug, minutes, start, info):
        self.slug, self.minutes, self.start = slug, minutes, start
        self.end = start + minutes * 60
        self.tokens = info["tokens"]                  # {"up": id, "down": id}
        self.side_of = {v: k for k, v in self.tokens.items()}
        self.condition = info.get("conditionId")
        self.tick = float(info.get("tick") or 0.01)
        self.min_size = float(info.get("min_size") or 5)
        # Settles on Chainlink's 60 s TWAP at the start (price to beat) and the end (final price).
        self.twap = True
        self.twap_flag = bool(info.get("twap", True))
        self.twap_s = int(info.get("twap_s") or 60)
        self.fee_rate = info.get("fee_rate")
        self.books = {"up": {"bids": {}, "asks": {}}, "down": {"bids": {}, "asks": {}}}
        self.book_ok = False
        self.orders = []                              # resting/cancelling paper orders
        self.trade_credit = {}                        # (side, px) -> [ms, shares printed, shrink not seen yet]
        self.shrinks = []                             # [ms, (side, px), shares, level size before] unexplained
        self.shares = {"up": 0.0, "down": 0.0}
        self.cost = {"up": 0.0, "down": 0.0}
        self.fills = 0
        self.fee_equiv = 0.0                          # maker fills, for the rebate estimate
        self.fees_paid = 0.0                          # taker fills
        self.strike = None                            # price to beat (official when available)
        self.strike_src = None
        self.strike_est = None                        # our own Chainlink TWAP estimate
        self.close_official = None                    # Polymarket's finalPrice (event metadata)
        self.strike_official = None                   # Polymarket's priceToBeat (event metadata)
        self.next_price_poll = 0.0
        self.price_poll_fails = 0
        self.fair = None                              # model P(up)
        self.fair_blend = None
        self.last_quote_ms = 0.0
        self.last_taker_ms = {"up": 0.0, "down": 0.0}
        self.quotes = {"up": [], "down": []}          # what we want resting (for the UI)
        self.state = "pending"                        # pending | quoting | closed | settled
        self.winner = None
        self.winner_src = None
        self.ws_status = "idle"
        self.last_book_ms = 0.0

    # book helpers
    def best_bid(self, side):
        b = self.books[side]["bids"]
        return max(b) if b else None

    def best_ask(self, side):
        a = self.books[side]["asks"]
        return min(a) if a else None

    def mid(self, side):
        b, a = self.best_bid(side), self.best_ask(side)
        if b is None or a is None:
            return None
        return (b + a) / 2

    def total_cost(self):
        return self.cost["up"] + self.cost["down"]

    def pnl_if(self, winner):
        return self.shares[winner] - self.total_cost()


# --------------------------------------------------------------------------
class PaperBot:
    CONFIG = BotConfig            # subclasses (other strategies) swap in their own config dataclass
    STRATEGY = "market_maker"

    def __init__(self, bot_id, cfg=None):
        """`cfg` (a dict) builds an offline instance for backend/bots/replay.py: no DB row, no history."""
        self.bot_id = bot_id
        self.data_dir = BOTS_DIR / bot_id
        self.offline = cfg is not None
        self.clock = time.time                               # replay swaps in the recording's clock
        if self.offline:
            self.name, self.account = bot_id, 0
            self.cfg = self.CONFIG.from_dict(cfg)
        else:
            row = db.one("SELECT * FROM bots WHERE id = ?", (bot_id,))
            if not row:
                raise ValueError(f"bot '{bot_id}' not found")
            self.name = row["name"]
            self.cfg = self.CONFIG.from_dict(db.loads(row["config"], {}))
            self.account = int(row["account"] or 1)
        self.lock = threading.RLock()
        self.running = False
        self._thread = None
        self._loop = None
        self._stop = None
        self.started_at = None
        self.clock_offset_ms = None
        self._oid = 0
        self._wq = queue.Queue()
        self._writer_started = False

        # price state
        self.cb = {"mid": None, "bid": None, "ask": None, "ts_ms": None, "recv_ms": None}
        self._cb_mt = {"bid": None, "ask": None}             # quotes rebuilt from the matches channel (fallback)
        self.cb_hist = collections.deque(maxlen=4000)        # (recv_ms, mid) - fine-grained for jump guard
        self.cb_1s = collections.deque(maxlen=1800)          # (sec, mid) - for vol & charts
        self.cl = collections.deque(maxlen=4000)             # (ts_ms, value) Chainlink ticks
        self.cl_1s = {}                                      # sec -> last Chainlink value that second
        self.basis = None                                    # chainlink - coinbase, rolling median
        self._basis_obs = collections.deque(maxlen=90)
        self.bn = {"mid": None, "recv_ms": None}             # Binance BTCUSDT best bid/ask mid
        self.bn_hist = collections.deque(maxlen=4000)        # (recv_ms, mid)
        self.basis_bn = None                                 # chainlink - binance, rolling median
        self._basis_bn_obs = collections.deque(maxlen=90)
        self._cl_exact = set()                               # seconds with a tick stamped exactly on them
        self.var_s = None                                    # per-second variance of log returns (EWMA)
        self.jump_until_ms = 0.0

        # markets
        self.markets = {}                                    # slug -> Market
        self._info_cache = {}
        self._ws_tasks = {}

        # accounting
        self.fills = collections.deque(maxlen=3000)
        self.settled = []                                     # window records
        self.markouts = collections.deque(maxlen=5000)       # (signed cents)
        self._pending_markouts = collections.deque()
        self.totals = {"volume": 0.0, "maker_volume": 0.0, "taker_volume": 0.0, "fills": 0, "maker_fills": 0,
                       "taker_fills": 0, "rebate_est": 0.0, "fees_paid": 0.0}
        # paper account: cash moves on fills (-cost) and on settlement (+payout +rebate)
        self.cash = self.cfg.bankroll
        self._orphans = {}                                   # slug -> record of a window cut off by a restart
        self.day_key = None
        self.day_start_value = None                          # today's loss counts from here (midnight, or a reset)
        self.day_hit_at = None                               # when the daily loss limit stopped entries today
        self._last_equity_write = 0.0
        self.account_since = None

        # telemetry
        self.status = {"coinbase": "stopped", "binance": "stopped", "chainlink": "stopped", "clob": "stopped",
                       "errors": []}
        self._err_seen = {}                                  # error kind -> [last written, repeats held back]
        # latency samples (ms unless *_us): feed = exchange timestamp -> received (clock-corrected);
        # decide_us = message received -> new quotes decided; e2e_* = exchange timestamp -> decision done
        self.lat = {k: collections.deque(maxlen=20000) for k in
                    ("coinbase", "chainlink", "clob", "decide_us", "loop", "e2e_coinbase", "e2e_book")}
        self.counts = collections.Counter()
        self._snapshot = {}
        self._snap_n = 0
        self._lat_cache, self._lat_ts = {}, 0.0
        self.publish_state = False                           # process mode: write state.json for the UI
        self.state_path = self.data_dir / "state.json"
        self.control_path = self.data_dir / "control.json"
        self._control_seen = None
        self._last_sec = 0
        if not self.offline:
            self._load_history()

    # ------------------------------------------------------------------ public
    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.started_at = time.time()
        if not self._writer_started:
            self._writer_started = True
            threading.Thread(target=self._writer_loop, daemon=True, name="bot-writer").start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bot-engine")
        self._thread.start()

    def stop(self):
        with self.lock:
            self.running = False
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)

    def set_config(self, **kw):
        names = {f.name for f in dataclasses.fields(self.CONFIG)}
        d = dataclasses.asdict(self.cfg)
        d.update({k: v for k, v in kw.items() if k in names})
        cfg = self.CONFIG(**d)
        # keep stored keys this (possibly older) code does not know: they belong to the next version
        stored = db.loads((db.one("SELECT config FROM bots WHERE id = ?", (self.bot_id,)) or {}).get("config"), {})
        extra = {k: v for k, v in stored.items() if k not in names}
        db.x("UPDATE bots SET config = ? WHERE id = ?", (db.dumps({**extra, **cfg.to_dict()}), self.bot_id))
        self.cfg = cfg   # atomic swap; the engine reads self.cfg on every decision

    def reset(self, bankroll=None):
        """Start a fresh paper account with `bankroll` (default: the configured one). Old results stay in
        the DB under the previous account number."""
        if bankroll is not None:
            self.set_config(bankroll=float(bankroll))
        with self.lock:
            self.account = new_account(self.bot_id, self.cfg.bankroll)
            self.cash = self.cfg.bankroll
            self._orphans.clear()
            self.day_start_value = self.cfg.bankroll
            self.day_hit_at = None
            self.account_since = time.time()
            self.fills.clear()
            self.settled.clear()
            self.markouts.clear()
            for k in self.totals:
                self.totals[k] = 0.0 if isinstance(self.totals[k], float) else 0
            for m in self.markets.values():
                m.shares = {"up": 0.0, "down": 0.0}
                m.cost = {"up": 0.0, "down": 0.0}
                m.fills, m.fee_equiv, m.fees_paid = 0, 0.0, 0.0
        for m in self.markets.values():
            m.orders = []

    def snapshot(self):
        return self._snapshot

    # ------------------------------------------------------------------ loop
    def _run(self):
        try:
            try:
                import uvloop                          # Linux: faster event loop (not available on Windows)
                asyncio.run(self._main(), loop_factory=uvloop.new_event_loop)
            except ImportError:
                asyncio.run(self._main())
        except Exception as e:
            self._err(f"engine crashed: {type(e).__name__}: {e}")
        finally:
            self.running = False
            self.status.update(coinbase="stopped", chainlink="stopped", clob="stopped")
            self._build_snapshot()

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        feeds = [self._coinbase(), self._chainlink()]
        if self.cfg.spot_source != "coinbase":
            feeds.append(self._binance())
        tasks = [asyncio.create_task(c) for c in (*feeds, self._scheduler(), self._ticker(), self._clock(),
                                                  self._resolver(), self._official_prices())]
        await self._stop.wait()
        for t in tasks + list(self._ws_tasks.values()):
            t.cancel()
        self._ws_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _err(self, msg):
        """Keep the last 20 errors for the UI and write them to bot.log (the process's stdout); one line per
        kind of error per ERR_REPEAT_S, with a count of the repeats that were held back."""
        now = time.time()
        errs = self.status["errors"]
        errs.append((now, msg))
        del errs[:-20]
        kind = msg.split(":", 1)[0][:60]
        seen = self._err_seen.get(kind)
        if seen and now - seen[0] < ERR_REPEAT_S:
            seen[1] += 1
            return
        held = f" (+{seen[1]} similar in the last {now - seen[0]:.0f} s)" if seen and seen[1] else ""
        self._err_seen[kind] = [now, 0]
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR {msg}{held}", flush=True)

    def _corr(self, ms):
        return ms + (self.clock_offset_ms or 0.0)

    async def _clock(self):
        """Clock error vs true time: right away, after 1 min (a just-booted server is still stepping its clock),
        then every 5 min. Feed latencies and the fast player's match times use it."""
        n = 0
        while True:
            off = await asyncio.to_thread(ntp_offset_ms)
            if off is not None:
                self.clock_offset_ms = off
                n += 1
            await asyncio.sleep(60 if n < 2 else 300)

    # ------------------------------------------------------------------ feeds
    async def _coinbase(self):
        """BTC-USD ticker. Coinbase allows only ~10 ticker subscriptions per product per IP (every bot and the
        recorder on one server each take one) and refuses the rest with an error message on an open, then silent,
        socket. Refused -> the `matches` channel on the same socket: the same trades, one message per match."""
        import websockets
        backoff = 1
        while True:
            try:
                self.status["coinbase"] = "connecting"
                async with websockets.connect(COINBASE_WS, ping_interval=15, ping_timeout=15, max_size=None, max_queue=None,
                                              open_timeout=10, close_timeout=WS_CLOSE_S, compression=None) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"],
                                              "channels": ["ticker"]}))
                    self.status["coinbase"] = "connected"
                    backoff = 1
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        if self._on_coinbase(raw) == "limit":
                            self._err("coinbase: ticker refused (subscription limit for this IP); using matches")
                            await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"],
                                                      "channels": ["matches"]}))
                            self.status["coinbase"] = "connected (matches)"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status["coinbase"] = "reconnecting"
                self._err(f"coinbase: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20)

    def _on_coinbase(self, raw):
        """Returns "limit" when Coinbase refused the ticker subscription; raises on any other error message."""
        d = orjson.loads(raw)
        t = d.get("type")
        if t == "ticker":
            self._on_coinbase_msg(d)
        elif t in ("match", "last_match"):
            self._on_coinbase_match(d)
        elif t == "error":
            why = f"{d.get('message')}: {d.get('reason')}"
            if "subscription limit" in why and "ticker" in why:
                return "limit"
            raise RuntimeError(why)
        return None

    def _on_coinbase_match(self, d):
        """A trade from the matches channel as a ticker message. The maker's side tells which quote traded: a
        resting buy (side "buy") is the best bid, a resting sell the best ask; the other side is the last seen,
        pushed one cent away if the book moved through it. `last_match` (sent on subscribe) only seeds them."""
        try:
            px = float(d["price"])
        except (KeyError, TypeError, ValueError):
            return
        q = self._cb_mt
        if d.get("side") == "buy":
            q["bid"] = px
            if q["ask"] is None or q["ask"] <= px:
                q["ask"] = round(px + 0.01, 2)
        else:
            q["ask"] = px
            if q["bid"] is None or q["bid"] >= px:
                q["bid"] = round(px - 0.01, 2)
        if d.get("type") == "match":
            self._on_coinbase_msg({"type": "ticker", "price": px, "best_bid": q["bid"], "best_ask": q["ask"],
                                   "time": d.get("time")})

    def _on_coinbase_msg(self, d):
        recv = self.clock() * 1000.0
        t0 = time.perf_counter()
        if d.get("type") != "ticker":
            return
        bid, ask = float(d["best_bid"]), float(d["best_ask"])
        mid = (bid + ask) / 2
        try:
            ts = datetime.fromisoformat(d["time"].replace("Z", "+00:00")).timestamp() * 1000.0
            if self.clock_offset_ms is not None:
                self.lat["coinbase"].append(self._corr(recv) - ts)
        except Exception:
            ts = recv
        prev = self.cb["mid"]
        self.cb.update(mid=mid, bid=bid, ask=ask, ts_ms=ts, recv_ms=recv)
        self.cb_hist.append((recv, mid))
        self.counts["coinbase"] += 1
        if prev is not None:
            self._jump_check(self.cb_hist, mid, recv)
        if mid != prev:
            self._requote_all(recv)
            self.lat["decide_us"].append((time.perf_counter() - t0) * 1e6)
            if self.clock_offset_ms is not None and ts != recv:
                self.lat["e2e_coinbase"].append(self._corr(self.clock() * 1000.0) - ts)

    async def _binance(self):
        import websockets
        backoff = 1
        while True:
            try:
                self.status["binance"] = "connecting"
                async with websockets.connect(BINANCE_WS, ping_interval=15, ping_timeout=15, max_size=None, max_queue=None,
                                              open_timeout=10, close_timeout=WS_CLOSE_S, compression=None) as ws:
                    self.status["binance"] = "connected"
                    backoff = 1
                    while True:
                        self._on_binance_msg(orjson.loads(await asyncio.wait_for(ws.recv(), timeout=20)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status["binance"] = "reconnecting"
                self._err(f"binance: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20)

    def _jump_check(self, hist, mid, recv):
        """Jump guard: a move of jump_bps against the price ~1 s ago pulls all quotes for jump_cooldown_ms."""
        ref = None
        for t, v in reversed(hist):
            if recv - t >= 1000:
                ref = v
                break
        if ref and abs(mid / ref - 1) * 1e4 >= self.cfg.jump_bps:
            if recv >= self.jump_until_ms:
                self.counts["jumps"] += 1
            self.jump_until_ms = recv + self.cfg.jump_cooldown_ms

    def _on_binance_msg(self, d):
        try:
            mid = (float(d["b"]) + float(d["a"])) / 2
        except (KeyError, TypeError, ValueError):
            return
        recv = self.clock() * 1000.0
        prev = self.bn["mid"]
        self.bn.update(mid=mid, recv_ms=recv)
        self.bn_hist.append((recv, mid))
        self.counts["binance"] += 1
        if self.cfg.spot_source == "coinbase":
            return
        if prev is not None:
            self._jump_check(self.bn_hist, mid, recv)
        if mid != prev:
            self._requote_all(recv)

    async def _chainlink(self):
        import websockets
        subs = [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": CL_SYMBOL})}]
        backoff = 1
        while True:
            try:
                self.status["chainlink"] = "connecting"
                async with websockets.connect(RTDS_URL, ping_interval=10, ping_timeout=10, max_size=None, max_queue=None,
                                              open_timeout=10, close_timeout=WS_CLOSE_S, compression=None) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
                    self.status["chainlink"] = "connected"
                    backoff = 1
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        self._on_chainlink(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status["chainlink"] = "reconnecting"
                self._err(f"chainlink: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20)

    def _on_chainlink(self, raw):
        if (CL_SYMBOL.encode() if isinstance(raw, bytes) else CL_SYMBOL) not in raw:
            return
        try:
            self._on_chainlink_msg(orjson.loads(raw))
        except ValueError:
            return

    def _on_chainlink_msg(self, d):
        recv = self.clock() * 1000.0
        p = d.get("payload") or {}
        if p.get("symbol") != CL_SYMBOL:
            return
        pts = []
        if isinstance(p.get("data"), list):          # backlog on subscribe
            pts = [(float(x["timestamp"]), float(x["value"])) for x in p["data"]]
        elif "value" in p:
            pts = [(float(p.get("timestamp") or recv), float(p["value"]))]
            if self.clock_offset_ms is not None:
                self.lat["chainlink"].append(self._corr(recv) - pts[0][0])
        for ts, v in sorted(pts):
            if self.cl and ts <= self.cl[-1][0]:
                continue
            self.cl.append((ts, v))
            self.cl_1s[int(ts // 1000)] = v
            if ts % 1000 == 0:
                self._cl_exact.add(int(ts // 1000))
            self.counts["chainlink"] += 1
            # basis vs Coinbase at (about) the same moment; only live ticks, not the backlog
            if len(pts) == 1 and self.cb["mid"] is not None:
                cbv = self._cb_at(ts)
                if cbv is not None:
                    self._basis_obs.append(v - cbv)
                    obs = sorted(self._basis_obs)
                    self.basis = obs[len(obs) // 2]
            if len(pts) == 1 and self.bn["mid"] is not None:
                bnv = self._at_time(self.bn_hist, ts)
                if bnv is not None:
                    self._basis_bn_obs.append(v - bnv)
                    obs = sorted(self._basis_bn_obs)
                    self.basis_bn = obs[len(obs) // 2]
        if len(self.cl_1s) > 4000:
            for k in sorted(self.cl_1s)[:1000]:
                self.cl_1s.pop(k, None)
                self._cl_exact.discard(k)

    def _cb_at(self, ts_ms):
        return self._at_time(self.cb_hist, ts_ms)

    @staticmethod
    def _at_time(hist, ts_ms):
        for t, v in reversed(hist):
            if t <= ts_ms:
                return v
        return None

    # ------------------------------------------------------------------ markets
    def _fetch_info(self, slug):
        js = HTTP.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10).json()
        if not js:
            return None
        m = js[0]
        outs = [str(o).lower() for o in json.loads(m.get("outcomes") or "[]")]
        toks = json.loads(m.get("clobTokenIds") or "[]")
        cmc = m.get("cryptoMarketConfig") or {}
        fs = m.get("feeSchedule") or {}
        return {"tokens": dict(zip(outs, toks)), "conditionId": m.get("conditionId"),
                "tick": m.get("orderPriceMinTickSize") or 0.01, "min_size": m.get("orderMinSize") or 5,
                "twap": cmc.get("twapEnabled", True), "twap_s": cmc.get("twapLookbackSeconds") or 60,
                "fee_rate": fs.get("rate"), "rebate_rate": fs.get("rebateRate")}

    async def _scheduler(self):
        """Keeps current + next window of each length resolved and its book socket open."""
        while True:
            try:
                now = time.time()
                want = set()
                for mins in self.cfg.windows:
                    L = mins * 60
                    s = int(now) // L * L
                    want.add((mins, s))
                    if s + L - now <= PREOPEN_S:
                        want.add((mins, s + L))
                for mins, s in want:
                    slug = f"btc-updown-{mins}m-{s}"
                    m = self.markets.get(slug)
                    if m is None:
                        info = self._info_cache.get(slug)
                        if info is None:
                            try:
                                info = await asyncio.to_thread(self._fetch_info, slug)
                            except Exception as e:
                                self._err(f"gamma {slug}: {e}"[:200])
                                info = None
                            if not info or "up" not in info["tokens"]:
                                continue
                            self._info_cache[slug] = info
                        m = Market(slug, mins, s, info)
                        orphan = self._orphans.pop(slug, None)
                        if orphan:                       # restarted mid-window: keep what we held
                            m.shares = {"up": orphan["up_sh"], "down": orphan["down_sh"]}
                            m.cost = {"up": orphan["up_cost"], "down": orphan["down_cost"]}
                            m.fills = orphan["fills"]
                            m.fee_equiv = orphan["rebate_est"] / max(self.cfg.rebate_share, 1e-9)
                        self.markets[slug] = m
                    if slug not in self._ws_tasks and time.time() < m.end + 3:
                        self._ws_tasks[slug] = asyncio.create_task(self._book_ws(m))
                # close sockets of finished windows; drop settled markets after a while
                for slug, m in list(self.markets.items()):
                    if now > m.end + 3 and slug in self._ws_tasks:
                        self._ws_tasks.pop(slug).cancel()
                        m.ws_status = "closed"
                    if m.state == "settled" and now > m.end + 120:
                        self.markets.pop(slug, None)
                        self._info_cache.pop(slug, None)
                n = sum(1 for m in self.markets.values() if m.ws_status == "connected")
                self.status["clob"] = f"{n} book{'s' if n != 1 else ''} live" if n else "waiting"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._err(f"scheduler: {type(e).__name__}: {e}"[:200])
            await asyncio.sleep(0.5)

    async def _book_ws(self, m):
        import websockets
        backoff = 0.1
        while time.time() < m.end + 3:
            try:
                m.ws_status = "connecting"
                async with websockets.connect(CLOB_WS, ping_interval=None, max_size=None, max_queue=None, open_timeout=10,
                                              close_timeout=WS_CLOSE_S, compression="deflate") as ws:
                    await ws.send(json.dumps({"assets_ids": list(m.tokens.values()), "type": "market"}))
                    m.ws_status = "connected"
                    backoff, last_ping = 0.1, time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            self._on_book(m, raw)
                        except asyncio.TimeoutError:
                            pass
                        if time.time() - last_ping > 9:
                            await ws.send("PING")
                            last_ping = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                m.ws_status = "reconnecting"
                m.book_ok = False
                self._err(f"book {m.slug}: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5)

    def _on_book(self, m, raw):
        if raw in ("PONG", b"PONG"):
            return
        try:
            d = orjson.loads(raw)
        except ValueError:
            return
        self._on_book_msg(m, d)

    def _on_book_msg(self, m, d):
        recv = self.clock() * 1000.0
        t0 = time.perf_counter()
        changed = False
        ex_ts = None
        for e in d if isinstance(d, list) else [d]:
            et = e.get("event_type")
            side = m.side_of.get(e.get("asset_id"))
            ts = e.get("timestamp")
            if ts:
                ex_ts = max(ex_ts or 0.0, float(ts))
            if ts and self.clock_offset_ms is not None:
                self.lat["clob"].append(self._corr(recv) - float(ts))
            self.counts["clob"] += 1
            if et == "book" and side:
                bk = m.books[side]
                old = bk["bids"]
                bk["bids"] = {float(x["price"]): float(x["size"]) for x in e.get("bids", [])}
                bk["asks"] = {float(x["price"]): float(x["size"]) for x in e.get("asks", [])}
                m.book_ok = bool(m.books["up"]["bids"] or m.books["up"]["asks"])
                for px in {round(o.price, 4) for o in m.orders if o.side == side and not o.taker}:
                    self._level_change(m, side, px, old.get(px, 0.0), bk["bids"].get(px, 0.0), recv)
                changed = True
            elif et == "price_change":
                for c in e.get("price_changes", []):
                    s = m.side_of.get(c.get("asset_id"))
                    if not s:
                        continue
                    bk = m.books[s]["bids" if c.get("side") == "BUY" else "asks"]
                    px, sz = float(c["price"]), float(c["size"])
                    old = bk.get(px, 0.0)
                    if sz <= 0:
                        bk.pop(px, None)
                    else:
                        bk[px] = sz
                    if c.get("side") == "BUY":
                        self._level_change(m, s, round(px, 4), old, max(sz, 0.0), recv)
                m.book_ok = True
                changed = True
            elif et == "last_trade_price" and side:
                self._on_print(m, side, float(e["price"]), float(e["size"]), str(e.get("side") or "").upper(),
                               self._print_ms(recv, ts))
            elif et == "tick_size_change" and side:
                try:
                    m.tick = float(e.get("new_tick_size") or m.tick)
                except (TypeError, ValueError):
                    pass
        m.last_book_ms = recv
        if changed:
            self._requote(m, recv)
            self.lat["decide_us"].append((time.perf_counter() - t0) * 1e6)
            if ex_ts and self.clock_offset_ms is not None:
                self.lat["e2e_book"].append(self._corr(self.clock() * 1000.0) - ex_ts)

    # ------------------------------------------------------------------ fill simulation
    def _print_ms(self, recv, ts):
        """When a print is judged against our orders: on receipt (subclasses: at its match time)."""
        return recv

    def _cancel_lat(self):
        """Time for a cancel to take effect (subclasses may set it apart from the order latency)."""
        return self.cfg.latency_ms

    def _on_print(self, m, side, price, size, taker_side, now):
        """A real trade on `side` at `price`. Decide which of our resting bids it would have hit."""
        lat = self._cancel_lat()
        if taker_side == "SELL":                 # the bid level the taker consumed
            self._match_print(m, (side, round(price, 4)), size, now)
        elif taker_side == "BUY":
            self._match_print(m, ("down" if side == "up" else "up", round(1.0 - price, 4)), size, now)
        cands = []
        for o in m.orders:
            if o.taker or o.remaining <= 1e-9 or not o.live(now, lat):
                continue
            if o.side == side and taker_side == "SELL":
                eq = price                       # taker sold our token
            elif o.side != side and taker_side == "BUY":
                eq = round(1.0 - price, 6)       # taker bought the other token: mint against our bid
            else:
                continue
            if o.price + 1e-9 >= eq:
                cands.append((o, eq))
        if not cands:
            return
        left = size
        cands.sort(key=lambda x: (-x[0].price, x[0].placed_ms))
        for o, eq in cands:
            if left <= 1e-9:
                break
            if o.price > eq + 1e-9:              # we were the better price: filled before that maker
                take = min(o.remaining, left)
            else:                                # same level: queue ahead goes first
                q = min(o.queue, left)
                o.queue -= q
                left -= q
                take = min(o.remaining, left)
            if take > 1e-9:
                left -= take
                self._fill(m, o, take, o.price, now, maker=True)
        m.orders = [o for o in m.orders if o.remaining > 1e-9]

    @staticmethod
    def _at(m, side, px):
        return [o for o in m.orders if o.side == side and not o.taker and abs(o.price - px) < 1e-9]

    def _level_change(self, m, side, px, old, new, now):
        """A bid level changed size. Growth is orders joining behind us. A shrink is a trade or cancels: it is
        first matched against prints already seen there; the rest waits LEVEL_MATCH_MS for its print
        (_match_print) and otherwise counts as cancels (_settle_levels). Prints alone move us up the queue."""
        if new >= old - 1e-9:
            return
        key, d = (side, px), old - new
        cr = m.trade_credit.get(key)
        if cr:
            use = min(d, cr[1])
            cr[1] -= use
            d -= use
            if cr[1] <= 1e-9:
                m.trade_credit.pop(key, None)
        ours = self._at(m, side, px)
        if d > 1e-9 and ours:
            m.shrinks.append([now, key, d, old])
        # never more shares ahead than the level holds, plus shrinks whose print may still arrive
        cap = new + sum(sh[2] for sh in m.shrinks if sh[1] == key)
        for o in ours:
            o.queue = min(o.queue, cap)

    @staticmethod
    def _match_print(m, key, size, now):
        """A trade consumed `size` at bid level `key`: it explains a shrink seen before it, or is remembered so
        the shrink arriving after it is not counted a second time."""
        left = size
        for sh in m.shrinks:
            if sh[1] == key and left > 1e-9:
                use = min(sh[2], left)
                sh[2] -= use
                left -= use
        if m.shrinks:
            m.shrinks = [sh for sh in m.shrinks if sh[2] > 1e-9]
        if left > 1e-9:
            cr = m.trade_credit.setdefault(key, [now, 0.0])
            cr[0] = now
            cr[1] += left

    def _settle_levels(self, m, now):
        """Shrinks no print explained within LEVEL_MATCH_MS are cancels, spread pro rata over the level: only
        the part ahead of us moves us up."""
        if m.shrinks:
            keep = []
            for sh in m.shrinks:
                t, (side, px), d, before = sh
                if now - t < LEVEL_MATCH_MS:
                    keep.append(sh)
                    continue
                for o in self._at(m, side, px):
                    if o.placed_ms <= t and before > 1e-9:
                        o.queue = max(0.0, o.queue - d * min(1.0, o.queue / before))
            m.shrinks = keep
        if m.trade_credit:
            for k in [k for k, v in m.trade_credit.items() if now - v[0] > LEVEL_MATCH_MS]:
                m.trade_credit.pop(k)

    def _fill(self, m, o, shares, price, now, maker):
        rate = m.fee_rate if m.fee_rate is not None else self.cfg.fee_rate
        fee_eq = shares * rate * price * (1 - price)
        usd = shares * price
        got = shares
        if maker:
            m.fee_equiv += fee_eq
            rebate = fee_eq * self.cfg.rebate_share
        else:
            got = shares - fee_eq / price    # taker fee is taken in shares on buys
            m.fees_paid += fee_eq
            rebate = 0.0
        o.remaining -= shares
        m.shares[o.side] += got
        m.cost[o.side] += usd
        self.cash -= usd
        m.fills += 1
        t = self.totals
        t["volume"] += usd
        t["fills"] += 1
        if maker:
            t["maker_volume"] += usd
            t["maker_fills"] += 1
            t["rebate_est"] += rebate
        else:
            t["taker_volume"] += usd
            t["taker_fills"] += 1
            t["fees_paid"] += fee_eq
        rec = {"ts": now / 1000.0, "slug": m.slug, "win": m.minutes, "side": o.side, "price": round(price, 4),
               "shares": round(got, 6), "usd": round(usd, 6), "maker": maker, "rebate": round(rebate, 6),
               "fee": 0.0 if maker else round(fee_eq, 6), "t_in": round(now / 1000.0 - m.start, 2),
               "fair": None if m.fair is None else round(m.fair if o.side == "up" else 1 - m.fair, 4),
               "btc": self.cb["mid"], "markout": None}
        self.fills.append(rec)
        self._pending_markouts.append((now + MARKOUT_S * 1000, m, o.side, price, rec))
        self._wq.put(("fill", rec))
        # an inventory change moves our skew: re-evaluate right away
        self._requote(m, now, force=True)

    # ------------------------------------------------------------------ model & quoting
    def _vol(self):
        v = self.var_s
        floor = (self.cfg.vol_floor / 1e4) ** 2
        return math.sqrt(max(v if v is not None else floor * 4, floor))

    def _spot(self):
        """Best estimate of where Chainlink is heading: exchange mid(s) + learned basis (`spot_source`)."""
        src = self.cfg.spot_source
        cb = self.cb["mid"] + self.basis if self.cb["mid"] is not None and self.basis is not None else None
        bn = self.bn["mid"] + self.basis_bn if self.bn["mid"] is not None and self.basis_bn is not None else None
        if src == "binance" and bn is not None:
            return bn
        if src == "blend" and bn is not None:
            return bn if cb is None else (bn + cb) / 2
        if cb is not None:
            return cb
        if self.cl:
            return self.cl[-1][1]
        return self.cb["mid"]

    def _avg_cl(self, t0, t1):
        """Time-weighted Chainlink average over [t0, t1) seconds, from 1 s samples (forward filled)."""
        d = self.cl_1s
        if not d:
            return None, 0
        last, tot, n = None, 0.0, 0
        for back in range(1, 31):                  # value carried in from before t0
            v = d.get(int(t0) - back)
            if v is not None:
                last = v
                break
        for sec in range(int(t0), int(t1)):
            v = d.get(sec)
            if v is not None:
                last = v
            if last is not None:
                tot += last
                n += 1
        return (tot / n if n else None), n

    def _twap(self, sec):
        """Chainlink 60 s TWAP at second `sec` from our exact-second ticks, or None if we lack them."""
        return twap_of({x: self.cl_1s[x] for x in twap_secs(sec) if x in self._cl_exact}, sec)

    def _strike(self, m):
        """Price to beat = Chainlink 60 s TWAP at the window's start, from our own ticks (ready ~1.5 s after the
        open, if the bot was running for the minute before it). Otherwise Polymarket's priceToBeat, which is
        published only once the previous window resolved."""
        if m.strike is not None:
            return m.strike
        if self.clock() < m.start:
            return None
        v = self._twap(m.start)
        if v is not None:
            m.strike, m.strike_src, m.strike_est = v, "Chainlink TWAP", v
        elif m.strike_official is not None:
            m.strike, m.strike_src = m.strike_official, "Polymarket"
        return m.strike

    def _cl_has(self, sec):
        """True when a Chainlink tick is stamped exactly at `sec` (not a carried-over value)."""
        return sec in self._cl_exact

    async def _official_prices(self):
        """Polymarket's priceToBeat (fallback / cross-check of our TWAP) and finalPrice (settles the window
        before Gamma marks it resolved), from the event metadata."""
        while True:
            try:
                now = time.time()
                for m in list(self.markets.values()):
                    need_open = m.strike_official is None and now >= m.start + 2 and now < m.end + 600
                    need_close = m.close_official is None and now >= m.end + 2 and now < m.end + 1800
                    if not (need_open or need_close) or now < m.next_price_poll:
                        continue
                    m.next_price_poll = now + 5.0
                    try:
                        off = await asyncio.to_thread(fetch_window_prices, m.slug)
                        m.price_poll_fails = 0
                    except Exception as e:
                        self.counts["price_api_errors"] += 1
                        m.price_poll_fails += 1
                        m.next_price_poll = now + min(5.0 * 2 ** m.price_poll_fails, 120.0)
                        if m.price_poll_fails == 1:
                            self._err(f"price api {m.slug}: {type(e).__name__}: {e}"[:200])
                        continue
                    op, cp = off.get("priceToBeat"), off.get("finalPrice")
                    if op is not None and m.strike_official is None:
                        m.strike_official = op
                        if m.strike_src == "Chainlink TWAP" and abs(op - m.strike) > 0.05:
                            self.counts["strike_mismatch"] += 1
                    if cp is not None and m.strike is not None:
                        m.close_official = cp
                        win = "up" if cp >= (m.strike_official if m.strike_official is not None else m.strike) else "down"
                        if m.winner != win:
                            if m.winner is not None:
                                self.counts["close_mismatch"] += 1
                            m.winner, m.winner_src = win, "official close"
                            for r in self.settled:
                                if r["slug"] == m.slug and not r.get("official"):
                                    self._apply_winner(r, win, "official close", final=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._err(f"official prices: {type(e).__name__}: {e}"[:200])
            await asyncio.sleep(0.5)

    def _model(self, m, now_s, S=None):
        """P(up) under a random walk with the TWAP settlement rule, given spot S (default: the
        Coinbase-led estimate)."""
        K = self._strike(m)
        S = self._spot() if S is None else S
        if K is None or S is None:
            return None
        sig = self._vol() * S                  # $ per sqrt(s)
        last = m.end - TWAP_END_LAG if m.twap else m.end      # the TWAP's last second (markets.twap_secs)
        tau = max(last - now_s, 0.0)
        W = float(m.twap_s) if m.twap else 0.0
        if not m.twap:
            mean, var = S, sig * sig * tau
        elif tau >= W:
            mean, var = S, sig * sig * (tau - W + W / 3.0)
        else:
            known, n = self._avg_cl(last - W + 1, min(now_s, last) + 1)   # TWAP seconds (last - W, last] seen so far
            if known is None:
                known = S
            mean = ((W - tau) * known + tau * S) / W
            var = (tau / W) ** 2 * sig * sig * tau / 3.0
        sd = math.sqrt(max(var, 1e-12))
        return min(max(_ncdf((mean - K) / sd), 0.003), 0.997)

    def _requote_all(self, now):
        for m in list(self.markets.values()):
            self._requote(m, now)

    def _requote(self, m, now, force=False):
        cfg = self.cfg
        if not force and now - m.last_quote_ms < cfg.min_requote_ms:
            return
        m.last_quote_ms = now
        now_s = now / 1000.0
        if now_s >= m.end:
            self._cancel_all(m, now)
            if m.state in ("pending", "quoting"):
                m.state = "closed"
            return
        p = self._model(m, now_s)
        p_lag = self._model(m, now_s, S=self.cl[-1][1]) if self.cl else None
        m.fair = p
        in_window = m.start + cfg.start_delay_s <= now_s <= m.end - cfg.stop_before_end_s
        if not in_window or p is None or not m.book_ok or now < self.jump_until_ms:
            m.quotes = {"up": [], "down": []}
            self._cancel_all(m, now)
            if in_window and m.state == "pending":
                m.state = "quoting"
            return
        m.state = "quoting"
        # Fair value anchored on the market (which is well calibrated), shifted by what Coinbase says
        # Chainlink is about to do (it leads by ~1.4 s), plus an optional pull towards the full model.
        mid_up = m.mid("up")
        if mid_up is None:
            fair_up = p
        else:
            lead = cfg.lead_weight * (p - p_lag) if p_lag is not None else 0.0
            fair_up = mid_up + lead + cfg.model_weight * (p - mid_up)
        fair_up = min(max(fair_up, 0.005), 0.995)
        m.fair_blend = fair_up
        imb = m.shares["up"] - m.shares["down"]
        for side, fair in (("up", fair_up), ("down", 1.0 - fair_up)):
            want = self._side_quotes(m, side, fair, now_s, imb)
            if cfg.maker_min_edge > -0.5:              # the pure model must see an edge at every resting price
                cap = (p if side == "up" else 1.0 - p) - cfg.maker_min_edge
                want = [(px, sz) for px, sz in want if px <= cap + 1e-9]
            m.quotes[side] = want
            self._reconcile(m, side, want, now)
        if cfg.taker_enabled:
            self._maybe_take(m, p if cfg.taker_fair == "model" else fair_up, now)

    def _side_quotes(self, m, side, fair, now_s, imb):
        """The resting bids wanted on one side: [(price, shares), ...]. Strategies override this."""
        cfg = self.cfg
        skew = cfg.skew_per_100 * imb / 100.0
        cost_cap = m.total_cost() >= cfg.max_window_cost
        heavy = (imb > 0) == (side == "up") and abs(imb) > 1e-9
        adj = skew if side == "up" else -skew          # long Up -> lower Up bids, raise Down bids
        top = min(fair - cfg.base_edge - adj, fair)    # skew may lower a bid, never lift it past fair
        ask = m.best_ask(side)
        if ask is not None:
            top = min(top, ask - m.tick)                # post-only: never cross
        bid = m.best_bid(side)
        if cfg.join_only and bid is not None:
            top = min(top, bid)                         # join the best bid, never improve it
        want = []
        if self._blocked(m, side, imb, heavy, cost_cap):
            return want
        for i in range(max(1, cfg.levels)):
            px = round(_floor_tick(top - i * cfg.level_step, m.tick), 4)
            if px < cfg.min_price or px > cfg.max_price:
                continue
            sz = float(cfg.sizes[min(i, len(cfg.sizes) - 1)])
            if cost_cap:                                # only rebalance up to flat
                sz = min(sz, abs(imb))
                if sz < m.min_size:
                    continue
            want.append((px, max(sz, m.min_size)))
        return want

    def _blocked(self, m, side, imb, heavy, cost_cap):
        cfg = self.cfg
        return (m.shares[side] >= cfg.max_side_shares or (heavy and abs(imb) >= cfg.max_imbalance)
                or (cost_cap and (heavy or abs(imb) < 1e-9)))

    def _reconcile(self, m, side, want, now):
        keep = {}
        for o in m.orders:
            if o.side != side or o.taker or o.cancel_ms is not None:
                continue
            k = round(o.price, 4)
            if k in keep or all(abs(o.price - px) > 1e-9 for px, _ in want):
                o.cancel_ms = now
                self.counts["cancels"] += 1
            else:
                keep[k] = o
        lat = self.cfg.latency_ms
        free = None
        for px, sz in want:
            if round(px, 4) in keep:
                continue
            if free is None:
                free = self.cash - self._reserved()
            if px * sz > free + 1e-9:                       # not enough free cash: like Polymarket, refuse
                self.counts["cash_limited"] += 1
                continue
            free -= px * sz
            self._oid += 1
            q = m.books[side]["bids"].get(px, 0.0)
            m.orders.append(Order(self._oid, side, px, sz, q, now, lat))
            self.counts["orders"] += 1
        # drop fully cancelled orders
        clat = self._cancel_lat()
        m.orders = [o for o in m.orders if o.cancel_ms is None or now < o.cancel_ms + clat]

    def _reserved(self):
        """Collateral locked by our resting bids (a cancel frees it only once it lands)."""
        return sum(o.remaining * o.price for mk in self.markets.values() for o in mk.orders)

    def _positions_value(self):
        """Mark-to-market of shares we hold in unsettled windows (book mid, else cost)."""
        v = 0.0
        for m in self.markets.values():
            if m.state == "settled":
                continue
            for side in ("up", "down"):
                sh = m.shares[side]
                if sh <= 0:
                    continue
                mid = m.mid(side)
                v += sh * mid if mid is not None else m.cost[side]
        v += sum(r["cost"] for r in self._orphans.values())
        v += sum(r["cost"] for r in self.settled if r.get("winner") is None)   # awaiting resolution
        return v

    def _cancel_all(self, m, now):
        lat = self._cancel_lat()
        for o in m.orders:
            if o.cancel_ms is None and not o.taker:
                o.cancel_ms = now
                self.counts["cancels"] += 1
        m.orders = [o for o in m.orders if o.taker or now < o.cancel_ms + lat]

    def _maybe_take(self, m, p, now):
        cfg = self.cfg
        rate = m.fee_rate if m.fee_rate is not None else cfg.fee_rate
        for side, fair in (("up", p), ("down", 1 - p)):
            if now - m.last_taker_ms[side] < cfg.taker_cooldown_ms or m.shares[side] >= cfg.max_side_shares:
                continue
            ask = m.best_ask(side)
            if ask is None or ask > cfg.max_price:
                continue
            eff = ask / (1 - rate * (1 - ask))       # price per share after the fee taken in shares
            if fair - eff >= cfg.taker_edge and self.cash - self._reserved() >= ask * cfg.taker_size:
                m.last_taker_ms[side] = now
                self._oid += 1
                o = Order(self._oid, side, ask, cfg.taker_size, 0.0, now, cfg.latency_ms, taker=True)
                m.orders.append(o)
                self.counts["taker_orders"] += 1

    def _exec_takers(self, m, now):
        """Taker orders arrive `latency_ms` later and fill only against what is still offered."""
        for o in [o for o in m.orders if o.taker and now >= o.active_ms]:
            m.orders.remove(o)
            asks = sorted(m.books[o.side]["asks"].items())
            left = o.remaining
            for px, sz in asks:
                if px > o.price + 1e-9 or left <= 1e-9:
                    break
                take = min(sz, left)
                left -= take
                o2 = Order(o.id, o.side, px, take, 0.0, now, 0)
                self._fill(m, o2, take, px, now, maker=False)
            if left > 1e-9:
                self.counts["taker_missed"] += 1

    # ------------------------------------------------------------------ periodic work
    def _tick(self, now):
        """Time-driven work, every 50 ms: volatility, cancels landing, taker orders arriving, window open/close,
        settlement, markouts."""
        sec = int(now // 1000)
        if sec != self._last_sec and self.cb["mid"] is not None:
            if self.cb_1s and self.cb_1s[-1][0] == sec - 1:
                r = math.log(self.cb["mid"] / self.cb_1s[-1][1])
                self.var_s = r * r if self.var_s is None else 0.995 * self.var_s + 0.005 * r * r
            self.cb_1s.append((sec, self.cb["mid"]))
            self._last_sec = sec
        for m in list(self.markets.values()):
            self._settle_levels(m, now)
            if any(o.taker for o in m.orders):
                self._exec_takers(m, now)
            if m.state in ("pending", "quoting"):
                self._requote(m, now)      # time-driven: window open/close, cancels landing
            if m.state == "closed" and now / 1000.0 > m.end + 1.5 and (
                    self._cl_has(m.end) or m.close_official is not None or now / 1000.0 > m.end + 30):
                self._provisional_settle(m)
        if self._orphans:
            for slug, r in list(self._orphans.items()):
                if now / 1000.0 > r["start"] + r["win"] * 60 + 5:
                    self._orphans.pop(slug)
                    with self.lock:
                        self.settled.append(r)
                    self._wq.put(("window", r))
        if now / 1000.0 - self._last_equity_write >= 30:
            self._equity_tick(now / 1000.0)
        while self._pending_markouts and self._pending_markouts[0][0] <= now:
            _, m, side, price, rec = self._pending_markouts.popleft()
            mid = m.mid(side)
            if mid is None and m.state == "settled" and m.winner:
                mid = 1.0 if m.winner == side else 0.0
            if mid is not None:
                mk = (mid - price) * 100
                rec["markout"] = round(mk, 2)
                self.markouts.append(mk)
                self._wq.put(("markout", rec))

    async def _ticker(self):
        last_snap = 0.0
        while True:
            try:
                now = time.time() * 1000.0
                self._tick(now)
                if now - last_snap >= 250:
                    self._build_snapshot()
                    last_snap = now
                    self._snap_n += 1
                    if self.publish_state and self._snap_n % 2 == 0:
                        self._wq.put(("state", self._snapshot))
                    self._check_control()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._err(f"ticker: {type(e).__name__}: {e}"[:200])
            t_sleep = time.perf_counter()
            await asyncio.sleep(0.05)
            self.lat["loop"].append(max(0.0, (time.perf_counter() - t_sleep - 0.05) * 1000.0))  # loop stall, ms

    def portfolio_value(self):
        return self.cash + self._positions_value()

    def _daily_limit_usd(self):
        """$ the bot may lose in a day (strategies with max_daily_loss_frac), else None."""
        frac = getattr(self.cfg, "max_daily_loss_frac", None)
        return None if frac is None else frac * self.cfg.bankroll

    def daily_loss_hit(self, eq=None):
        """True once today's loss passes the daily limit; records when that first happened today."""
        lim = self._daily_limit_usd()
        if lim is None or self.day_start_value is None:
            return False
        eq = self.portfolio_value() if eq is None else eq
        if eq >= self.day_start_value - lim:
            return False
        if self.day_hit_at is None:
            self.day_hit_at = time.time()
        return True

    def reset_day(self):
        """Clear today's loss: the daily limit counts from the current value. The equity row written now is what
        _load_history restores after a restart (the API logs the reset in bot_changes)."""
        with self.lock:
            self.day_start_value = self.portfolio_value()
            self.day_hit_at = None
        self._equity_tick(time.time())

    @staticmethod
    def _day_key(ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def _equity_tick(self, now_s):
        """Record the portfolio value (for the PnL chart) and roll the 'today' baseline at midnight."""
        self._last_equity_write = now_s
        v = self.portfolio_value()
        dk = self._day_key(now_s)
        if self.day_key is not None and self.day_key != dk:
            self.day_start_value = v
            self.day_hit_at = None
        self.day_key = dk
        self._wq.put(("equity", {"ts": round(now_s, 1), "v": round(v, 4), "cash": round(self.cash, 4)}))

    def _provisional_settle(self, m):
        """Settle on the Chainlink 60 s TWAP at the end second (what Polymarket settles on); its finalPrice and the
        resolved market confirm it later."""
        m.state = "settled"
        final = self._twap(m.end)
        if final is not None and m.strike is not None:
            m.winner, m.winner_src = ("up" if final >= m.strike else "down"), "Chainlink TWAP"
        elif m.close_official is not None and m.strike is not None:
            m.winner, m.winner_src = ("up" if m.close_official >= m.strike else "down"), "official close"
        if m.fills == 0:
            return
        rec = self._window_record(m)
        rec["final"] = final if final is not None else m.close_official
        rec["credited"] = 0.0
        if m.winner:
            rec["credited"] = round(m.shares[m.winner] + rec["rebate_est"], 6)
            self.cash += rec["credited"]
        rec["settled_ts"] = self.clock()
        with self.lock:
            self.settled.append(rec)
        self._wq.put(("window", rec))

    def _window_record(self, m):
        cost = m.total_cost()
        pnl = m.pnl_if(m.winner) if m.winner else None
        rebate = m.fee_equiv * self.cfg.rebate_share
        up, dn = m.shares["up"], m.shares["down"]
        return {"slug": m.slug, "win": m.minutes, "start": m.start, "condition": m.condition,
                "fills": m.fills, "up_sh": round(up, 4), "down_sh": round(dn, 4),
                "up_cost": round(m.cost["up"], 4), "down_cost": round(m.cost["down"], 4), "cost": round(cost, 4),
                "pair_cost": round(m.cost["up"] / up + m.cost["down"] / dn, 4) if up > 0 and dn > 0 else None,
                "strike": m.strike, "strike_src": m.strike_src, "strike_official": m.strike_official, "winner": m.winner, "winner_src": m.winner_src,
                "trading_pnl": None if pnl is None else round(pnl, 4), "rebate_est": round(rebate, 4),
                "fees_paid": round(m.fees_paid, 4),
                "net_pnl": None if pnl is None else round(pnl + rebate, 4), "final": None, "official": False}

    async def _resolver(self):
        """Replace provisional outcomes with Polymarket's official result."""
        while True:
            try:
                for rec in [r for r in list(self.settled) if not r.get("official")]:
                    age = time.time() - (rec["start"] + rec["win"] * 60)
                    if age < 20:
                        continue
                    win = await asyncio.to_thread(self._official, rec["slug"])
                    if win:
                        if win != rec["winner"]:
                            self.counts["outcome_corrections"] += 1
                        self._apply_winner(rec, win, "resolved", final=True)
                    elif age > 3600:
                        rec["official"] = True          # give up waiting; keep the estimate
                        self._wq.put(("window", rec))
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._err(f"resolver: {type(e).__name__}: {e}"[:200])
            await asyncio.sleep(10)

    def _apply_winner(self, rec, win, src, final):
        rec["winner"], rec["winner_src"] = win, src
        pay = rec["up_sh"] if win == "up" else rec["down_sh"]
        credit = round(pay + (rec.get("rebate_est") or 0.0), 6)
        self.cash += credit - (rec.get("credited") or 0.0)
        rec["credited"] = credit
        rec["trading_pnl"] = round(pay - rec["cost"], 4)
        rec["net_pnl"] = round(rec["trading_pnl"] + rec["rebate_est"], 4)
        if final:
            rec["official"] = True
        self._wq.put(("window", rec))

    @staticmethod
    def _official(slug):
        js = HTTP.get(f"{GAMMA}/markets", params={"slug": slug, "closed": "true"}, timeout=10).json()
        if not js:
            return None
        m = js[0]
        try:
            outs = [str(o).lower() for o in json.loads(m.get("outcomes") or "[]")]
            px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except (TypeError, ValueError):
            return None
        for o, p in zip(outs, px):
            if p >= 0.99:
                return o
        return None

    # ------------------------------------------------------------------ persistence
    def _writer_loop(self):
        """Persists fills / windows / equity to the DB and the UI state file, off the engine thread."""
        conn = None
        while True:
            kind, rec = self._wq.get()
            try:
                if kind == "state":
                    if self._wq.qsize() > 50:        # never let UI state starve the records
                        continue
                    write_text_atomic(self.state_path, orjson.dumps(rec, default=_jsonable,
                                                                   option=orjson.OPT_NON_STR_KEYS).decode())
                    continue
                conn = conn or db.conn()
                acc = self.account
                if kind == "equity":
                    conn.execute("INSERT INTO bot_equity (bot_id, account, ts, v, cash) VALUES (?,?,?,?,?)",
                                 (self.bot_id, acc, rec["ts"], rec["v"], rec["cash"]))
                elif kind == "fill":
                    conn.execute("INSERT INTO bot_fills (bot_id, account, ts, slug, win, side, price, shares, usd, "
                                 "maker, rebate, fee, t_in, fair, btc, markout) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (self.bot_id, acc, rec["ts"], rec["slug"], rec["win"], rec["side"], rec["price"],
                                  rec["shares"], rec["usd"], int(rec["maker"]), rec["rebate"], rec["fee"], rec["t_in"],
                                  rec["fair"], rec["btc"], rec["markout"]))
                elif kind == "markout":
                    conn.execute("UPDATE bot_fills SET markout = ? WHERE bot_id = ? AND account = ? AND ts = ? "
                                 "AND slug = ? AND side = ?", (rec["markout"], self.bot_id, acc, rec["ts"],
                                                                rec["slug"], rec["side"]))
                else:
                    conn.execute("INSERT OR REPLACE INTO bot_windows (bot_id, account, slug, start, win, net_pnl, data) "
                                 "VALUES (?,?,?,?,?,?,?)", (self.bot_id, acc, rec["slug"], rec["start"], rec["win"],
                                                            rec.get("net_pnl"), db.dumps(rec)))
            except Exception as e:
                self._err(f"writer: {type(e).__name__}: {e}"[:200])

    def _check_control(self):
        """Commands from the UI (process mode): {"id", "action": stop|reset|reset_day|config, "config": {...}}.
        Whatever is in the file at startup is old and ignored."""
        try:
            mt = self.control_path.stat().st_mtime_ns
        except OSError:
            mt = None
        if self._control_seen is not None and mt == self._control_seen[0]:
            return
        cmd = {}
        if mt is not None:
            try:
                cmd = json.loads(self.control_path.read_text(encoding="utf-8"))
            except Exception:
                return
        first = self._control_seen is None
        prev_id = None if first else self._control_seen[1]
        self._control_seen = (mt, cmd.get("id"))
        if first or cmd.get("id") in (None, prev_id):
            return
        act = cmd.get("action")
        if act == "stop":
            self._stop.set()
        elif act == "reset":
            self.reset(cmd.get("bankroll"))
        elif act == "reset_day":
            self.reset_day()
        elif act == "config":
            self.set_config(**(cmd.get("config") or {}))

    def _load_history(self):
        """Rebuild the paper account of the current account number from the DB."""
        acc = (self.bot_id, self.account)
        seen = {r["slug"]: db.loads(r["data"], {}) for r in
                db.q("SELECT slug, data FROM bot_windows WHERE bot_id = ? AND account = ?", acc)}
        cutoff = time.time() - 3 * 86400
        for r in db.q("SELECT ts, slug, win, side, price, shares, usd, maker, rebate, fee, t_in, fair, btc, markout "
                      "FROM bot_fills WHERE bot_id = ? AND account = ? AND ts >= ? ORDER BY ts", acc + (cutoff,)):
            r["maker"] = bool(r["maker"])
            self.fills.append(r)
        # windows whose fills were saved but which never settled (bot stopped mid-window)
        open_fills = db.q("SELECT slug, win, side, shares, usd, rebate, fee FROM bot_fills WHERE bot_id = ? "
                          "AND account = ? AND slug NOT IN (SELECT slug FROM bot_windows WHERE bot_id = ? "
                          "AND account = ?)", acc + acc)
        by_slug = collections.defaultdict(list)
        for r in open_fills:
            by_slug[r["slug"]].append(r)
        for slug, fl in by_slug.items():
            start = int(slug.rsplit("-", 1)[-1])
            up = [r for r in fl if r["side"] == "up"]
            dn = [r for r in fl if r["side"] == "down"]
            rec = {"slug": slug, "win": fl[0]["win"], "start": start, "condition": None, "fills": len(fl),
                   "up_sh": round(sum(r["shares"] for r in up), 4), "down_sh": round(sum(r["shares"] for r in dn), 4),
                   "up_cost": round(sum(r["usd"] for r in up), 4), "down_cost": round(sum(r["usd"] for r in dn), 4),
                   "pair_cost": None, "strike": None, "strike_src": None, "strike_official": None, "winner": None,
                   "winner_src": None, "trading_pnl": None,
                   "rebate_est": round(sum(r.get("rebate") or 0 for r in fl), 4),
                   "fees_paid": round(sum(r.get("fee") or 0 for r in fl), 4), "net_pnl": None, "final": None,
                   "official": False, "credited": 0.0, "restored": True}
            rec["cost"] = round(rec["up_cost"] + rec["down_cost"], 4)
            if time.time() < start + rec["win"] * 60 + 5:
                self._orphans[slug] = rec
            else:
                seen[slug] = rec
        self.settled = sorted(seen.values(), key=lambda r: r["start"])
        # cash = bankroll + everything paid out - everything spent
        self.cash = self.cfg.bankroll
        for r in self.settled:
            if "credited" not in r:
                if r.get("winner"):
                    pay = r["up_sh"] if r["winner"] == "up" else r["down_sh"]
                    r["credited"] = pay + (r.get("rebate_est") or 0.0)
                else:
                    r["credited"] = 0.0
            self.cash += r["credited"] - (r.get("cost") or 0.0)
        for r in self._orphans.values():
            self.cash -= r["cost"]
        t = self.totals
        for r in self.settled:
            t["rebate_est"] += r.get("rebate_est") or 0.0
            t["fees_paid"] += r.get("fees_paid") or 0.0
            t["volume"] += r.get("cost") or 0.0
            t["fills"] += r.get("fills") or 0
        row = db.one("SELECT account_since FROM bots WHERE id = ?", (self.bot_id,))
        self.account_since = (row or {}).get("account_since")
        today0 = time.mktime(time.strptime(self._day_key(time.time()), "%Y-%m-%d"))
        before = db.one("SELECT v FROM bot_equity WHERE bot_id = ? AND account = ? AND ts < ? ORDER BY ts DESC "
                        "LIMIT 1", acc + (today0,))
        if self.account_since is None:
            first = db.one("SELECT ts FROM bot_equity WHERE bot_id = ? AND account = ? ORDER BY ts LIMIT 1", acc)
            self.account_since = first["ts"] if first else (self.settled[0]["start"] if self.settled else time.time())
        self.day_start_value = before["v"] if before else self.cfg.bankroll
        # a daily limit reset today (Reset daily limit): count from the value at the reset instead
        rs = db.one("SELECT ts FROM bot_changes WHERE bot_id = ? AND kind = 'daily_reset' AND ts >= ? "
                    "ORDER BY ts DESC LIMIT 1", (self.bot_id, today0))
        if rs:
            at = db.one("SELECT v FROM bot_equity WHERE bot_id = ? AND account = ? AND ts <= ? ORDER BY ts DESC "
                        "LIMIT 1", acc + (rs["ts"] + 5,))
            if at:
                self.day_start_value = at["v"]
        self.day_key = self._day_key(time.time())

    # ------------------------------------------------------------------ UI snapshot
    def _build_snapshot(self):
        now = time.time()
        mk = []
        for m in sorted(self.markets.values(), key=lambda m: (m.minutes, m.start)):
            if m.state == "settled" and now > m.end + 30:
                continue
            up, dn = m.shares["up"], m.shares["down"]
            mk.append({
                "slug": m.slug, "win": m.minutes, "start": m.start, "end": m.end, "state": m.state,
                "ws": m.ws_status, "strike": m.strike, "strike_src": m.strike_src, "strike_official": m.strike_official,
                "fair": m.fair, "fair_blend": m.fair_blend,
                "twap": m.twap, "tick": m.tick,
                "book": {s: {"bid": m.best_bid(s), "ask": m.best_ask(s),
                             "bids": sorted(m.books[s]["bids"].items(), key=lambda x: -x[0])[:6],
                             "asks": sorted(m.books[s]["asks"].items())[:6]} for s in ("up", "down")},
                "quotes": {s: list(m.quotes[s]) for s in ("up", "down")},
                "resting": [(o.side, o.price, o.remaining, o.queue, o.cancel_ms is not None) for o in m.orders
                            if not o.taker],
                "shares": dict(m.shares), "cost": dict(m.cost), "fills": m.fills,
                "rebate_est": m.fee_equiv * self.cfg.rebate_share, "fees_paid": m.fees_paid,
                "pnl_up": m.pnl_if("up"), "pnl_down": m.pnl_if("down"),
                "pair_cost": (m.cost["up"] / up + m.cost["down"] / dn) if up > 0 and dn > 0 else None,
                "winner": m.winner,
            })
        settled = list(self.settled)
        done = [r for r in settled if r.get("trading_pnl") is not None]
        reserved = self._reserved()
        pos_value = self._positions_value()
        portfolio = self.cash + pos_value
        positions, orders = [], []
        for m in sorted(self.markets.values(), key=lambda m: (m.end, m.minutes)):
            if m.state == "settled":
                continue
            for side in ("up", "down"):
                sh = m.shares[side]
                if sh > 1e-9:
                    mid = m.mid(side)
                    positions.append({"slug": m.slug, "win": m.minutes, "start": m.start, "end": m.end, "side": side,
                                      "shares": sh, "cost": m.cost[side], "avg": m.cost[side] / sh, "price": mid,
                                      "value": sh * mid if mid is not None else None, "state": m.state})
            for o in m.orders:
                if o.taker:
                    continue
                orders.append({"slug": m.slug, "win": m.minutes, "start": m.start, "end": m.end, "side": o.side,
                               "price": o.price, "size": o.size, "remaining": o.remaining, "queue": o.queue,
                               "age": now - o.placed_ms / 1000.0,
                               "status": "cancelling" if o.cancel_ms is not None else
                                         ("live" if now * 1000 >= o.active_ms else "sending")})
        for r in list(self._orphans.values()) + [r for r in settled if r.get("winner") is None]:
            for side in ("up", "down"):
                sh = r[f"{side}_sh"]
                if sh > 1e-9:
                    positions.append({"slug": r["slug"], "win": r["win"], "start": r["start"],
                                      "end": r["start"] + r["win"] * 60, "side": side, "shares": sh,
                                      "cost": r[f"{side}_cost"], "avg": r[f"{side}_cost"] / sh, "price": None,
                                      "value": None, "state": "resolving"})
        wins = [r.get("net_pnl") for r in done if r.get("net_pnl") is not None]
        day0 = self.day_start_value if self.day_start_value is not None else self.cfg.bankroll
        account = {"bankroll": self.cfg.bankroll, "cash": self.cash, "reserved": reserved,
                   "available": self.cash - reserved, "positions_value": pos_value, "portfolio": portfolio,
                   "pnl_total": portfolio - self.cfg.bankroll, "pnl_today": portfolio - day0, "day_start": day0,
                   "biggest_win": max(wins) if wins else None, "since": self.account_since,
                   "windows": len(done) + sum(1 for m in self.markets.values() if m.fills and m.state != "settled")}
        lim = self._daily_limit_usd()
        if lim is not None:
            tomorrow = time.mktime(time.strptime(self._day_key(now + 86400), "%Y-%m-%d"))
            account["daily_limit"] = {"limit": lim, "lost": max(0.0, day0 - portfolio), "hit": self.daily_loss_hit(portfolio),
                                      "hit_at": self.day_hit_at, "resets_at": tomorrow}
        trading = sum(r["trading_pnl"] for r in done)
        rebates_settled = sum(r.get("rebate_est") or 0.0 for r in done)
        series = [(r["start"] + r["win"] * 60, r["trading_pnl"], r.get("rebate_est") or 0.0)
                  for r in sorted(done, key=lambda r: r["start"] + r["win"] * 60)]
        open_cost = sum(m.total_cost() for m in self.markets.values() if m.state != "settled")
        if now - self._lat_ts >= 5.0 or not self._lat_cache:      # sorting 20k samples: not every frame
            self._lat_ts = now
            lat_stats = {}
            for k, v in self.lat.items():
                xs = sorted(list(v)[-LAT_STATS_N:])      # recent samples only: sorting 20k x 8 stalled the loop
                if xs:
                    lat_stats[k] = {"n": len(xs), "mean": sum(xs) / len(xs), "p50": xs[len(xs) // 2],
                                    "p90": xs[int(len(xs) * .9)], "p99": xs[min(len(xs) - 1, int(len(xs) * .99))],
                                    "max": xs[-1]}
            self._lat_cache = lat_stats
        lat_stats = self._lat_cache
        lat = {k: (st["p50"], st["p90"]) for k, st in lat_stats.items()}
        cl_last = self.cl[-1] if self.cl else None
        self._snapshot = {
            "ts": now, "bot_id": self.bot_id, "name": self.name, "strategy": self.STRATEGY, "mode": "paper",
            "account_no": self.account, "running": self.running, "started_at": self.started_at, "cfg": dataclasses.asdict(self.cfg),
            "status": dict(self.status), "errors": list(self.status["errors"])[-8:],
            "coinbase": dict(self.cb), "chainlink": cl_last, "basis": self.basis, "spot": self._spot(),
            "vol_bps": self._vol() * 1e4, "jump_active": now * 1000 < self.jump_until_ms,
            "markets": mk, "fills": list(self.fills)[-100:], "settled": settled[-300:],
            "totals": dict(self.totals), "trading_pnl": trading, "rebates_settled": rebates_settled, "pnl_series": series,
            "windows_done": len(done),
            "windows_won": sum(1 for r in done if r["trading_pnl"] > 0),
            "open_cost": open_cost, "markout_avg": (sum(self.markouts) / len(self.markouts)) if self.markouts else None,
            "markout_n": len(self.markouts), "lat": lat, "lat_stats": lat_stats, "counts": dict(self.counts),
            "uptime_s": (now - self.started_at) if self.started_at else 0.0,
            "clock_offset_ms": self.clock_offset_ms, "pid": os.getpid(),
            "cb_series": list(self.cb_1s)[-300:],
            "cl_series": sorted((k, v) for k, v in self.cl_1s.items() if k >= now - 300),
            "account": account, "positions": positions, "orders": orders,
        }


def new_account(bot_id, bankroll):
    """Bump the bot's account number (fresh paper account); returns the new number."""
    db.x("UPDATE bots SET account = account + 1, account_since = ?, bankroll = ? WHERE id = ?",
         (time.time(), float(bankroll), bot_id))
    return int(db.one("SELECT account FROM bots WHERE id = ?", (bot_id,))["account"])

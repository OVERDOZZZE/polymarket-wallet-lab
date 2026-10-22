"""Real-time monitoring of wallets - built to minimise detection delay (copy-trading research).

Sources, fastest first:
  1. Polymarket RTDS websocket (wss://ws-live-data.polymarket.com), topic activity/trades.
     A single global stream of every matched trade *with the trader's proxy wallet*, pushed
     ~0.3-0.6 s after the match. Filtered locally, so one connection serves any number of
     wallets. Callbacks fire inside the receive loop, before anything else is done.
  2. Same socket, topic crypto_prices_chainlink: the Chainlink stream the Up/Down markets
     resolve on (used for the live strike / distance-to-strike).
  3. CLOB market websocket: full order book + deltas for the current (and next) window.
  4. data-api /activity polling (every POLL_S): backup for missed trades and the only source
     for REDEEM / MERGE / SPLIT. Its delay vs the websocket is measured and reported.
  5. data-api /positions and /value polling for open positions.

Note: another wallet's resting (unfilled) orders are NOT observable - the CLOB order book is
anonymous and the orders API only returns your own orders. Fills are visible the moment they
match; that's the earliest public signal of what a wallet is doing.

Usage from a bot:
    from backend.traders.live import LiveHub
    hub = LiveHub(); hub.start()
    hub.watch("0xWALLET", on_event=lambda ev: print(ev))   # ev is a dict, see _trade_event()

Captured events of watched traders are stored in the DB table trader_events.
"""
import asyncio
import collections
import json
import queue
import re
import threading
import time

import requests

from .. import db
from ..config import CLOB_MARKET_WS as CLOB_WS, DATA_API, GAMMA, RTDS_WS as RTDS_URL
from ..polymarket.markets import ASSETS, TWAP_S, twap_of, window_bounds, window_slug
from ..util import ntp_offset_ms, pct as _pct

CHAINLINK = {k: v["chainlink"] for k, v in ASSETS.items()}
POLL_S = 2.5
POSITIONS_S = 15
STALE_S = 20

_ENVELOPE_TS = re.compile(r'"timestamp":\s*(\d{13})\s*,\s*"topic"')
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Mozilla/5.0 (pmtool live)"


def now_ms():
    return time.time() * 1000.0










# --------------------------------------------------------------------------
class Watch:
    """Live state for one watched wallet (one per profile)."""

    def __init__(self, wallet, profile_id=None, asset="BTC", window_minutes=5, record=False):
        self.wallet = wallet.lower()
        self.profile_id = profile_id          # trader id
        self.asset, self.window_minutes = asset, window_minutes
        self.record = record                  # store events in trader_events
        self.events = collections.deque(maxlen=5000)
        self.keys = {}
        self.callbacks = []
        self.since = time.time()
        self.n_ws = self.n_poll = 0
        self.trade_lag = collections.deque(maxlen=500)   # recv - trade timestamp (ms, clock-corrected)
        self.poll_behind = collections.deque(maxlen=500)  # poll detection - ws detection (ms)
        self.positions, self.positions_ts, self.value = [], 0.0, None
        self.poll_error = ""
        self.seeded = False
        self.occ_ws = collections.Counter()  # identical-fill counter for websocket events

    @staticmethod
    def key(ev):
        """Identity of a fill, minus multiplicity: one transaction can hold several identical
        fills (same size and price against different makers), so _emit adds an occurrence number."""
        return (str(ev.get("tx") or "").lower(), str(ev.get("asset") or ""), ev.get("kind"),
                ev.get("side") or "", round(float(ev.get("size") or 0), 4), round(float(ev.get("price") or 0), 6))


def _trade_event(p, recv_ms, server_ms, source):
    price, size = float(p.get("price") or 0), float(p.get("size") or 0)
    return {
        "kind": "TRADE", "source": source, "recv_ms": recv_ms, "server_ms": server_ms,
        "ts": int(p.get("timestamp") or 0), "wallet": str(p.get("proxyWallet") or "").lower(),
        "slug": p.get("slug") or p.get("eventSlug"), "title": p.get("title"),
        "conditionId": p.get("conditionId"), "asset": str(p.get("asset") or ""),
        "outcome": p.get("outcome"), "outcomeIndex": p.get("outcomeIndex"),
        "side": str(p.get("side") or "").upper(), "price": price, "size": size,
        "usdc": float(p.get("usdcSize") or price * size), "fee": p.get("fee"),
        "tx": p.get("transactionHash"), "name": p.get("name"),
    }


def _activity_event(r, recv_ms, source):
    kind = str(r.get("type") or "").upper()
    if kind == "TRADE":
        return _trade_event(r, recv_ms, None, source)
    return {"kind": kind, "source": source, "recv_ms": recv_ms, "server_ms": None,
            "ts": int(r.get("timestamp") or 0), "wallet": str(r.get("proxyWallet") or "").lower(),
            "slug": r.get("slug"), "title": r.get("title"), "conditionId": r.get("conditionId"),
            "asset": str(r.get("asset") or ""), "outcome": r.get("outcome"),
            "outcomeIndex": r.get("outcomeIndex"), "side": "", "price": float(r.get("price") or 0),
            "size": float(r.get("size") or 0), "usdc": float(r.get("usdcSize") or 0), "fee": None,
            "tx": r.get("transactionHash"), "name": r.get("name")}


# --------------------------------------------------------------------------
class LiveHub:
    def __init__(self):
        self.lock = threading.RLock()
        self.watches = {}            # key (profile id or wallet) -> Watch
        self.prices = {s: collections.deque(maxlen=20000) for s in CHAINLINK.values()}  # (ms, value)
        self.books = {}              # token id -> {"bids": {p: s}, "asks": {p: s}, "ts", "last"}
        self.mids = collections.defaultdict(lambda: collections.deque(maxlen=3000))  # token -> (ms, bid, ask)
        self.markets = {}            # slug -> {"tokens": {"up": id, "down": id}, "conditionId", "title"} or None
        self._market_retry = {}
        self.clob_assets = set()
        self.status = {"rtds": "stopped", "clob": "idle", "rtds_error": "", "clob_error": ""}
        self.stats = {"rtds_msgs": 0, "rtds_last_ms": 0.0, "rtds_connects": 0, "clob_connects": 0,
                      "clob_last_ms": 0.0, "started": None}
        self.feed_lag = collections.deque(maxlen=2000)  # envelope server ts -> receipt (ms, corrected)
        self.clock_offset_ms = None
        self._strike_cache = {}
        self._rtds_connected_ms = 0.0
        self._started = False
        self._wq = queue.Queue()

    # ---------------- public API
    def start(self):
        with self.lock:
            if self._started:
                return
            self._started = True
            self.stats["started"] = time.time()
        threading.Thread(target=self._run_loop, daemon=True, name="pm-live-ws").start()
        threading.Thread(target=self._poll_loop, daemon=True, name="pm-live-poll").start()
        threading.Thread(target=self._writer_loop, daemon=True, name="pm-live-writer").start()
        threading.Thread(target=self._clock_loop, daemon=True, name="pm-live-clock").start()

    def watch(self, wallet, profile_id=None, asset="BTC", window_minutes=5, record=None, on_event=None):
        key = profile_id or wallet.lower()
        record = bool(profile_id) if record is None else record
        with self.lock:
            w = self.watches.get(key)
            if w is None:
                w = Watch(wallet, profile_id, asset, window_minutes, record)
                self.watches[key] = w
            else:
                w.asset, w.window_minutes = asset, window_minutes
            if on_event and on_event not in w.callbacks:
                w.callbacks.append(on_event)
        self.start()
        return w

    def unwatch(self, key):
        with self.lock:
            self.watches.pop(key, None)

    def get(self, key):
        return self.watches.get(key)

    def is_watching(self, key):
        return key in self.watches

    def price_series(self, asset, since_ms=0):
        dq = self.prices.get(CHAINLINK.get(asset, ""), ())
        with self.lock:
            return [x for x in dq if x[0] >= since_ms]

    def last_price(self, asset):
        dq = self.prices.get(CHAINLINK.get(asset, ""))
        return dq[-1] if dq else None

    def price_at(self, asset, ts_ms):
        """Last Chainlink value at or before ts_ms (None if we joined later)."""
        dq = self.prices.get(CHAINLINK.get(asset, ""))
        if not dq or dq[0][0] > ts_ms:
            return None
        with self.lock:
            best = None
            for t, v in dq:
                if t > ts_ms:
                    break
                best = v
        return best

    def twap(self, asset, sec):
        """Chainlink 60 s TWAP at second `sec` (see markets.twap_of) - what Up/Down windows open and settle at -
        or None if we lack ticks for that minute."""
        dq = self.prices.get(CHAINLINK.get(asset, ""))
        if not dq:
            return None
        lo = (sec - TWAP_S) * 1000.0
        with self.lock:
            ticks = {}
            for ts, v in reversed(dq):
                if ts <= lo:
                    break
                if ts <= sec * 1000.0 and ts % 1000 == 0:
                    ticks[int(ts // 1000)] = v
        return twap_of(ticks, sec)

    def strike(self, asset, start):
        """(price, source) at window open: Chainlink 60 s TWAP if we have the ticks, else Binance 1s open."""
        v = self.twap(asset, start)
        if v is not None:
            return v, "chainlink twap"
        key = (asset, int(start))
        if key not in self._strike_cache:
            try:
                k = HTTP.get("https://api.binance.com/api/v3/klines", timeout=5, params={
                    "symbol": ASSETS[asset]["symbol"], "interval": "1s",
                    "startTime": int(start) * 1000, "limit": 1}).json()
                self._strike_cache[key] = float(k[0][1]) if k else None
            except Exception:
                self._strike_cache[key] = None
        v = self._strike_cache[key]
        return (v, "binance") if v is not None else (None, None)

    def book(self, token):
        with self.lock:
            b = self.books.get(token)
            if not b:
                return None
            return {"bids": sorted(b["bids"].items(), key=lambda x: -x[0]),
                    "asks": sorted(b["asks"].items(), key=lambda x: x[0]),
                    "ts": b.get("ts"), "last": b.get("last")}

    def market(self, slug):
        return self.markets.get(slug)

    def latency_summary(self):
        lag = list(self.feed_lag)
        return {"feed_p50": _pct(lag, .5), "feed_p90": _pct(lag, .9), "samples": len(lag),
                "clock_offset_ms": self.clock_offset_ms}

    # ---------------- internals
    def _corr(self, ms):
        return ms + (self.clock_offset_ms or 0.0)

    def _emit(self, w, ev, occ=None):
        """Match against events already seen, store, persist, fire callbacks. Returns True if new.
        `occ` numbers identical fills within one source (websocket: arrival order; poll: order
        within the polled batch) so the n-th identical fill from one source matches the n-th
        from the other, and genuine duplicates are never merged."""
        base = Watch.key(ev)
        with self.lock:
            if occ is None:
                occ = w.occ_ws[base]
                w.occ_ws[base] += 1
            k = base + (occ,)
            prev = w.keys.get(k)
            if prev is not None:
                if ev["source"] == "poll" and prev["source"] == "ws" and "poll_ms" not in prev:
                    prev["poll_ms"] = ev["recv_ms"]
                    w.poll_behind.append(ev["recv_ms"] - prev["recv_ms"])
                return False
            w.keys[k] = ev
            if len(w.keys) > 20000:
                for kk in list(w.keys)[:5000]:
                    w.keys.pop(kk, None)
            w.events.append(ev)
            if ev["source"] == "ws":
                w.n_ws += 1
                if ev.get("ts"):
                    w.trade_lag.append(self._corr(ev["recv_ms"]) - ev["ts"] * 1000)
            elif ev["source"] == "poll":
                w.n_poll += 1
        for cb in list(w.callbacks):
            try:
                cb(ev)
            except Exception:
                pass
        if w.record and w.profile_id and ev["source"] != "history":
            self._wq.put((w.profile_id, ev))
        return True

    def _writer_loop(self):
        """Batch events into the DB (one transaction per burst)."""
        while True:
            batch = [self._wq.get()]
            time.sleep(0.2)
            while not self._wq.empty() and len(batch) < 500:
                batch.append(self._wq.get_nowait())
            try:
                db.many("INSERT INTO trader_events (trader_id, recv_ms, ts, kind, source, side, outcome, price, "
                        "size, usdc, slug, tx, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(tid, ev["recv_ms"], ev.get("ts"), ev.get("kind"), ev.get("source"), ev.get("side"),
                          ev.get("outcome"), ev.get("price"), ev.get("size"), ev.get("usdc"), ev.get("slug"),
                          ev.get("tx"), db.dumps(ev)) for tid, ev in batch])
            except Exception:
                pass

    def _clock_loop(self):
        while True:
            off = ntp_offset_ms()
            if off is not None:
                self.clock_offset_ms = off
            time.sleep(600 if off is not None else 60)

    def _run_loop(self):
        asyncio.run(self._main())

    async def _main(self):
        await asyncio.gather(self._rtds(), self._clob(), self._market_tracker())

    # ---------------- RTDS: trades + chainlink
    async def _rtds(self):
        import websockets
        # filtered chainlink subscriptions only deliver a ~60s backlog; the unfiltered one
        # streams live updates for every symbol (~1/s each)
        subs = [{"topic": "activity", "type": "trades"},
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}] + [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": s})}
            for s in CHAINLINK.values()]
        backoff = 1
        while True:
            try:
                self.status["rtds"] = "connecting"
                async with websockets.connect(RTDS_URL, ping_interval=10, ping_timeout=10,
                                              max_size=None, open_timeout=10) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
                    self.status.update(rtds="connected", rtds_error="")
                    self._rtds_connected_ms = now_ms()
                    self.stats["rtds_connects"] += 1
                    backoff = 1
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=STALE_S)
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"no data for {STALE_S}s")
                        self._on_rtds(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status.update(rtds="reconnecting", rtds_error=f"{type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _on_rtds(self, raw):
        recv = now_ms()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        self.stats["rtds_msgs"] += 1
        self.stats["rtds_last_ms"] = recv
        m = _ENVELOPE_TS.search(raw)
        server_ms = float(m.group(1)) if m else None
        # skip the burst of buffered messages right after (re)connecting; it isn't steady-state latency
        if server_ms and self.clock_offset_ms is not None and recv - self._rtds_connected_ms > 5000:
            self.feed_lag.append(self._corr(recv) - server_ms)
        if '"activity"' in raw:
            if not self.watches:
                return
            low = raw.lower()
            hits = [w for w in list(self.watches.values()) if w.wallet in low]  # cheap pre-filter
            if not hits:
                return
            try:
                p = json.loads(raw).get("payload") or {}
            except ValueError:
                return
            wallet = str(p.get("proxyWallet") or "").lower()
            for w in hits:
                if w.wallet == wallet:
                    self._emit(w, _trade_event(p, recv, server_ms, "ws"))
        elif "crypto_prices" in raw:
            try:
                d = json.loads(raw)
            except ValueError:
                return
            p = d.get("payload") or {}
            sym = p.get("symbol")
            with self.lock:
                if "data" in p and isinstance(p["data"], list):  # backlog on subscribe
                    dq = self.prices.get(sym)
                    if dq is not None:
                        merged = dict(dq)
                        merged.update((float(x["timestamp"]), float(x["value"])) for x in p["data"])
                        dq.clear()
                        dq.extend(sorted(merged.items()))
                elif sym in self.prices and "value" in p:
                    dq = self.prices[sym]
                    t = float(p.get("timestamp") or server_ms or recv)
                    if not dq or t > dq[-1][0]:
                        dq.append((t, float(p["value"])))

    # ---------------- markets to follow (current + next window, and anything traded recently)
    def _wanted_slugs(self):
        out = set()
        now = time.time()
        for w in list(self.watches.values()):
            s, _ = window_bounds(w.window_minutes)
            L = w.window_minutes * 60
            out.add(window_slug(w.asset, w.window_minutes, s))
            out.add(window_slug(w.asset, w.window_minutes, s + L))
            for ev in list(w.events)[-50:]:
                if ev.get("slug") and ev.get("kind") == "TRADE" and now - ev.get("ts", 0) < 600:
                    out.add(ev["slug"])
        return out

    def _resolve_market(self, slug):
        js = HTTP.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10).json()
        if not js:
            return None
        m = js[0]
        toks = json.loads(m.get("clobTokenIds") or "[]")
        outs = [str(o).lower() for o in json.loads(m.get("outcomes") or "[]")]
        return {"tokens": dict(zip(outs, toks)), "conditionId": m.get("conditionId"),
                "title": m.get("question") or m.get("title"), "end": m.get("endDate")}

    async def _market_tracker(self):
        while True:
            try:
                want = self._wanted_slugs()
                for slug in want:
                    if slug in self.markets and self.markets[slug]:
                        continue
                    if time.time() < self._market_retry.get(slug, 0):
                        continue
                    try:
                        info = await asyncio.to_thread(self._resolve_market, slug)
                    except Exception:
                        info = None
                    self.markets[slug] = info
                    if info is None:
                        self._market_retry[slug] = time.time() + 20
                assets = set()
                for slug in want:
                    info = self.markets.get(slug)
                    if info:
                        assets.update(info["tokens"].values())
                if assets != self.clob_assets:
                    self.clob_assets = assets
                # forget markets that ended long ago
                for slug in [s for s in self.markets if s not in want]:
                    self.markets.pop(slug, None)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    # ---------------- CLOB order books
    async def _clob(self):
        import websockets
        backoff = 1
        while True:
            assets = set(self.clob_assets)
            if not assets:
                self.status["clob"] = "idle"
                await asyncio.sleep(0.5)
                continue
            try:
                self.status["clob"] = "connecting"
                async with websockets.connect(CLOB_WS, ping_interval=None, max_size=None,
                                              open_timeout=10) as ws:
                    await ws.send(json.dumps({"assets_ids": sorted(assets), "type": "market"}))
                    self.status.update(clob="connected", clob_error="")
                    self.stats["clob_connects"] += 1
                    backoff, last_ping, last_msg = 1, time.time(), time.time()
                    with self.lock:
                        for t in [t for t in self.books if t not in assets]:
                            self.books.pop(t, None)
                    while self.clob_assets == assets:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            last_msg = time.time()
                            self._on_clob(raw)
                        except asyncio.TimeoutError:
                            if time.time() - last_msg > 60:
                                raise ConnectionError("order book feed silent for 60s")
                        if time.time() - last_ping > 10:
                            await ws.send("PING")
                            last_ping = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status.update(clob="reconnecting", clob_error=f"{type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _on_clob(self, raw):
        if raw in ("PONG", b"PONG"):
            return
        recv = now_ms()
        self.stats["clob_last_ms"] = recv
        try:
            d = json.loads(raw)
        except ValueError:
            return
        for e in d if isinstance(d, list) else [d]:
            et = e.get("event_type")
            with self.lock:
                if et == "book":
                    tok = e.get("asset_id")
                    self.books[tok] = {
                        "bids": {float(x["price"]): float(x["size"]) for x in e.get("bids", [])},
                        "asks": {float(x["price"]): float(x["size"]) for x in e.get("asks", [])},
                        "ts": float(e.get("timestamp") or recv), "last": (self.books.get(tok) or {}).get("last")}
                    self._note_mid(tok, recv)
                elif et == "price_change":
                    for c in e.get("price_changes", []):
                        tok = c.get("asset_id")
                        b = self.books.get(tok)
                        if b is None:
                            continue
                        side = b["bids"] if c.get("side") == "BUY" else b["asks"]
                        px, sz = float(c["price"]), float(c["size"])
                        if sz <= 0:
                            side.pop(px, None)
                        else:
                            side[px] = sz
                        b["ts"] = float(e.get("timestamp") or recv)
                        self._note_mid(tok, recv)
                elif et == "last_trade_price":
                    b = self.books.get(e.get("asset_id"))
                    if b is not None:
                        b["last"] = (float(e.get("price") or 0), e.get("side"), float(e.get("timestamp") or recv))

    def _note_mid(self, tok, recv):
        b = self.books.get(tok)
        bid = max(b["bids"]) if b["bids"] else None
        ask = min(b["asks"]) if b["asks"] else None
        dq = self.mids[tok]
        if not dq or dq[-1][1:] != (bid, ask):
            if dq and recv - dq[-1][0] < 200:  # coalesce bursts
                dq[-1] = (recv, bid, ask)
            else:
                dq.append((recv, bid, ask))

    # ---------------- REST polling: backup trades, redeem/merge/split, positions
    def _poll_loop(self):
        while True:
            t0 = time.time()
            for w in list(self.watches.values()):
                try:
                    rows = HTTP.get(f"{DATA_API}/activity", timeout=10, params={
                        "user": w.wallet, "limit": 100, "sortBy": "TIMESTAMP", "sortDirection": "DESC"}).json()
                    if isinstance(rows, list):
                        recv = now_ms()
                        occ = collections.Counter()
                        for r in reversed(rows):
                            ev = _activity_event(r, recv, "poll")
                            # rows from before the watch started (the API runs ~10 s behind) are history;
                            # +2 s covers trades whose websocket message landed just before watch()
                            if not w.seeded or ev["ts"] < w.since + 2:
                                ev["source"] = "history"
                            base = Watch.key(ev)
                            self._emit(w, ev, occ[base])
                            occ[base] += 1
                        w.seeded = True
                    w.poll_error = ""
                except Exception as e:
                    w.poll_error = f"{type(e).__name__}: {e}"[:200]
                if time.time() - w.positions_ts > POSITIONS_S:
                    try:
                        pos = HTTP.get(f"{DATA_API}/positions", timeout=10, params={
                            "user": w.wallet, "limit": 200, "sizeThreshold": 0.01,
                            "sortBy": "CURRENT", "sortDirection": "DESC"}).json()
                        val = HTTP.get(f"{DATA_API}/value", params={"user": w.wallet}, timeout=10).json()
                        w.positions = pos if isinstance(pos, list) else []
                        w.value = (val[0] or {}).get("value") if isinstance(val, list) and val else None
                        w.positions_ts = time.time()
                    except Exception as e:
                        w.poll_error = f"positions: {type(e).__name__}: {e}"[:200]
            time.sleep(max(0.2, POLL_S - (time.time() - t0)))

"""The spike-taker paper bot: Binance spot mid + Polymarket's live BTC 5m books -> simulated taker buys.

Flow
  * markets(): every second, make sure the current and the next BTC 5m window are known (Gamma) and that the book
    socket is subscribed to their four tokens (reconnect when the set changes).
  * binance(): every bookTicker update -> mid; move = mid / (first mid within the last SIGNAL_WINDOW_MS) - 1.
    |move| >= THETA_BPS -> on_signal().
  * on_signal(): window time, cooldown, price range, staleness and budget checks; then, SIM_LATENCY_MS later,
    execute(): walk the favoured token's asks (the live local book at that moment) up to signal ask + SLIPPAGE,
    taker fee per level, fill-and-kill (the rest is dropped).
  * settler(): after a window ends, Gamma's outcome prices give the winner; winning shares pay $1.
Everything is written to logs/spikebot.db (Ledger) and logs/spikebot.log."""
import asyncio
import collections
import json
import logging
import math
import os
import time

import requests
import websockets

from . import config as C
from .ledger import Ledger

log = logging.getLogger("spikebot")
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "spikebot-paper/1.0"


def now_ms():
    return time.time() * 1000.0


class Book:
    """One token's order book from the CLOB market channel, plus a short history of its best ask."""

    def __init__(self):
        self.bids, self.asks = {}, {}
        self.ask_hist = collections.deque(maxlen=4000)   # (ms, best ask)
        self.updated = 0.0

    def snapshot(self, msg):
        self.bids = {float(x["price"]): float(x["size"]) for x in msg.get("bids", [])}
        self.asks = {float(x["price"]): float(x["size"]) for x in msg.get("asks", [])}
        self._touch()

    def change(self, ch):
        side = self.bids if ch["side"] == "BUY" else self.asks
        p, s = float(ch["price"]), float(ch["size"])
        if s <= 0:
            side.pop(p, None)
        else:
            side[p] = s
        self._touch()

    def _touch(self):
        self.updated = now_ms()
        a = self.best_ask()
        if not self.ask_hist or self.ask_hist[-1][1] != a:
            self.ask_hist.append((self.updated, a))

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def ask_at(self, ms):
        """Best ask as it was at time ms (None if unknown)."""
        out = None
        for t, a in reversed(self.ask_hist):
            if t <= ms:
                return a
            out = a
        return out if self.ask_hist and self.ask_hist[0][0] <= ms else None


class Bot:
    def __init__(self):
        self.ledger = Ledger()
        self.markets = {}            # start -> {"slug", "up", "down"}
        self.books = {}              # token -> Book
        self.subscribed = frozenset()
        self.ws_task = None
        self.mids = collections.deque()   # (ms, mid)
        self.last_bn = 0.0
        self.cooldown = {}           # slug -> earliest next entry (ms)
        self.pending = set()         # slugs with an order in flight
        self.last_skip_log = 0.0
        self.stats = collections.Counter()
        self.stop = asyncio.Event()
        self.live = None
        if C.LIVE:
            from .live import LiveTrader
            self.live = LiveTrader(self.ledger, log)

    # ------------------------------------------------------------------ markets and book feed
    def fetch_market(self, start):
        slug = f"btc-updown-5m-{start}"
        r = HTTP.get(f"{C.GAMMA}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        ev = r.json()
        if not ev:
            return None
        m = ev[0]["markets"][0]
        toks = dict(zip([o.lower() for o in json.loads(m["outcomes"])], json.loads(m["clobTokenIds"])))
        return {"slug": slug, "up": toks["up"], "down": toks["down"]}

    async def markets_loop(self):
        while not self.stop.is_set():
            try:
                w = int(time.time()) // C.WINDOW_S * C.WINDOW_S
                for s in (w, w + C.WINDOW_S):
                    if s not in self.markets:
                        m = await asyncio.to_thread(self.fetch_market, s)
                        if m:
                            self.markets[s] = m
                            log.info("market %s up=%s.. down=%s..", m["slug"], m["up"][:8], m["down"][:8])
                for s in [s for s in self.markets if s < w - 3600]:
                    del self.markets[s]
                want = frozenset(t for s in (w, w + C.WINDOW_S) if s in self.markets
                                 for t in (self.markets[s]["up"], self.markets[s]["down"]))
                if want and (want != self.subscribed or self.ws_task is None or self.ws_task.done()):
                    if self.ws_task is not None:
                        self.ws_task.cancel()
                    self.subscribed = want
                    for t in list(self.books):
                        if t not in want:
                            del self.books[t]
                    self.ws_task = asyncio.create_task(self.book_ws(want))
            except Exception as e:
                log.warning("markets: %s: %s", type(e).__name__, e)
            await asyncio.sleep(1.0)

    async def book_ws(self, tokens):
        while not self.stop.is_set():
            # a book is only trusted from this connection's snapshot on: while the socket is down (Polymarket drops
            # slow consumers every few minutes) there is no book, so no signal can fill against frozen prices
            for t in tokens:
                self.books.pop(t, None)
            try:
                async with websockets.connect(C.CLOB_MARKET_WS, ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 24) as ws:
                    await ws.send(json.dumps({"assets_ids": sorted(tokens), "type": "market"}))
                    log.info("book socket subscribed to %d tokens", len(tokens))
                    async for raw in ws:
                        if raw == "PONG":
                            continue
                        msg = json.loads(raw)
                        for d in (msg if isinstance(msg, list) else [msg]):
                            et = d.get("event_type")
                            if et == "book":
                                self.books.setdefault(d["asset_id"], Book()).snapshot(d)
                            elif et == "price_change" or "price_changes" in d:
                                for ch in d.get("price_changes", []):
                                    b = self.books.get(ch["asset_id"])
                                    if b is not None:
                                        b.change(ch)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                for t in tokens:
                    self.books.pop(t, None)
                self.stats["book_reconnects"] += 1
                log.warning("book socket: %s: %s (reconnecting)", type(e).__name__, e)
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ signal feed
    async def binance_loop(self):
        while not self.stop.is_set():
            try:
                async with websockets.connect(C.BINANCE_WS, ping_interval=20, ping_timeout=20) as ws:
                    log.info("binance connected")
                    async for raw in ws:
                        d = json.loads(raw)
                        mid = (float(d["b"]) + float(d["a"])) / 2
                        t = now_ms()
                        self.last_bn = t
                        if self.mids and self.mids[-1][1] == mid:
                            continue
                        self.mids.append((t, mid))
                        while self.mids and self.mids[0][0] < t - C.SIGNAL_WINDOW_MS:
                            self.mids.popleft()
                        move = (mid / self.mids[0][1] - 1) * 1e4
                        if abs(move) >= C.THETA_BPS:
                            self.on_signal(t, move)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("binance: %s: %s (reconnecting)", type(e).__name__, e)
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ strategy
    def on_signal(self, t, move):
        start = int(t / 1000) // C.WINDOW_S * C.WINDOW_S
        m = self.markets.get(start)
        if m is None:
            return
        slug, tin = m["slug"], t / 1000 - start
        if slug in self.pending or t < self.cooldown.get(slug, 0):
            return
        side = "up" if move > 0 else "down"
        book = self.books.get(m[side])
        ask = book.best_ask() if book else None
        ask_prev = book.ask_at(t - C.SIGNAL_WINDOW_MS) if book else None
        reason = None
        if not (C.T_MIN_S <= tin < C.WINDOW_S - C.T_END_S):
            reason = "window_time"
        elif book is None or ask is None or ask_prev is None:
            reason = "no_book"
        elif t - book.updated > 30000:
            reason = "book_stale"
        elif not (C.P_MIN <= ask <= C.P_MAX):
            reason = "price_range"
        elif ask - ask_prev > C.MAX_REPRICE + 1e-9:
            reason = "already_repriced"
        elif self.ledger.window_spent(slug) >= C.MAX_WINDOW_USD - C.MIN_SHARES * ask:
            reason = "window_budget"
        if reason:
            self.stats[f"skip_{reason}"] += 1
            if t - self.last_skip_log > 1000 and reason != "window_time":
                self.last_skip_log = t
                self.ledger.signal(t / 1000, slug, tin, move, side, ask, ask_prev, "skip", reason)
            return
        self.pending.add(slug)
        self.stats["orders"] += 1
        loop = asyncio.get_running_loop()
        if self.live is not None:
            loop.create_task(self.live.buy(slug, side, ask, move, tin, t / 1000))
        loop.create_task(self.execute(t, m, side, move, ask, ask_prev, tin))
        loop.create_task(self.probe(t, m, side, move, ask))

    async def probe(self, t, m, side, move, ask):
        """Best ask of the favoured token 250 / 500 / 1000 / 2000 ms after each order's signal (the delay curve as
        seen from this machine), whatever the order did."""
        seen = []
        for ms in (250, 500, 1000, 2000):
            await asyncio.sleep(max(0.0, (t + ms - now_ms()) / 1000.0))
            b = self.books.get(m[side])
            seen.append(b.best_ask() if b else None)
        try:
            self.ledger.probe((t / 1000, m["slug"], side, move, ask, *seen))
        except Exception as e:
            log.warning("probe: %s", e)

    async def execute(self, t, m, side, move, ask, ask_prev, tin):
        slug = m["slug"]
        try:
            await asyncio.sleep(C.SIM_LATENCY_MS / 1000.0)
            want = max(C.MIN_SHARES, math.floor(C.ORDER_USD / ask))
            budget = min(C.ORDER_USD * 1.1, C.MAX_WINDOW_USD - self.ledger.window_spent(slug), self.ledger.cash)
            want = min(want, math.floor(budget / (ask * (1 + C.FEE_RATE * (1 - ask)))))
            limit = round(ask + C.SLIPPAGE, 3)
            book = self.books.get(m[side])
            levels, got, cost, fee = [], 0.0, 0.0, 0.0
            if want >= C.MIN_SHARES and book is not None:
                for p in sorted(book.asks):
                    if p > limit + 1e-9 or got >= want:
                        break
                    q = min(book.asks[p], want - got)
                    levels.append((p, q))
                    got += q
                    cost += p * q
                    fee += C.FEE_RATE * p * (1 - p) * q
            now = now_ms()
            if got <= 0:
                self.stats["missed"] += 1
                self.ledger.signal(t / 1000, slug, tin, move, side, ask, ask_prev, "miss",
                                   f"no ask <= {limit} after {C.SIM_LATENCY_MS} ms (best {book.best_ask() if book else None})")
                log.info("MISS %s %s move %+.2f bps ask %.3f -> best %s", slug, side, move, ask,
                         book.best_ask() if book else None)
                return
            self.ledger.fill({"ts": now / 1000, "signal_ts": t / 1000, "slug": slug, "tin": tin, "side": side,
                              "token": m[side], "move_bps": move, "ask_signal": ask, "ask_prev": ask_prev,
                              "limit_px": limit, "shares_req": want, "shares": got, "avg_px": cost / got,
                              "cost": cost, "fee": fee, "levels": levels})
            self.ledger.signal(t / 1000, slug, tin, move, side, ask, ask_prev, "fill", f"{got:.0f} sh @ {cost / got:.3f}")
            self.cooldown[slug] = t + C.COOLDOWN_S * 1000
            self.stats["fills"] += 1
            log.info("FILL %s %s t+%.0fs move %+.2f bps ask %.3f (prev %.3f) -> %.1f sh @ %.4f cost %.2f fee %.3f cash %.2f",
                     slug, side, tin, move, ask, ask_prev, got, cost / got, cost, fee, self.ledger.cash)
        except Exception as e:
            log.exception("execute: %s", e)
        finally:
            self.pending.discard(slug)

    # ------------------------------------------------------------------ settlement
    def fetch_winner(self, slug):
        r = HTTP.get(f"{C.GAMMA}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        ev = r.json()
        if not ev:
            return None
        m = ev[0]["markets"][0]
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        outs = [o.lower() for o in json.loads(m.get("outcomes") or "[]")]
        if m.get("closed") and len(prices) == 2 and max(prices) >= 0.99:
            return outs[prices.index(max(prices))]
        return None

    async def settler_loop(self):
        while not self.stop.is_set():
            try:
                if self.live is not None:
                    for slug in self.ledger.live_unsettled():
                        if time.time() < int(slug.rsplit("-", 1)[1]) + C.WINDOW_S + 30:
                            continue
                        winner = await asyncio.to_thread(self.fetch_winner, slug)
                        if winner:
                            self.ledger.live_settle(slug, winner)
                            log.info("LIVE SETTLE %s winner %s -> live %s", slug, winner, self.ledger.live_risk())
                for slug, start, up_sh, dn_sh, cost, fee in self.ledger.unsettled():
                    if time.time() < start + C.WINDOW_S + 30:
                        continue
                    winner = await asyncio.to_thread(self.fetch_winner, slug)
                    if winner:
                        pnl = self.ledger.settle(slug, winner)
                        log.info("SETTLE %s winner %s up %.1f down %.1f cost %.2f fee %.3f pnl %+.2f cash %.2f",
                                 slug, winner, up_sh, dn_sh, cost, fee, pnl, self.ledger.cash)
            except Exception as e:
                log.warning("settler: %s: %s", type(e).__name__, e)
            await asyncio.sleep(20)

    # ------------------------------------------------------------------ status, stop file, main
    async def status_loop(self):
        last_eq = 0.0
        while not self.stop.is_set():
            try:
                if C.STOP_PATH.exists():
                    log.info("stop file found: stopping")
                    self.stop.set()
                    break
                t = now_ms()
                snap = self.ledger.snapshot()
                if t - last_eq > 60000:
                    last_eq = t
                    self.ledger.record_equity()
                st = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), **snap,
                      "binance_age_ms": round(t - self.last_bn) if self.last_bn else None,
                      "books": {tok[:8]: round(t - b.updated) for tok, b in self.books.items()},
                      "windows": [self.markets[s]["slug"] for s in sorted(self.markets)][-2:],
                      "stats": dict(self.stats), "pid": os.getpid(),
                      "live": self.live.status() if self.live is not None else None}
                tmp = C.STATUS_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(st, indent=1))
                os.replace(tmp, C.STATUS_PATH)
            except Exception as e:
                log.warning("status: %s: %s", type(e).__name__, e)
            await asyncio.sleep(5)

    async def run(self):
        log.info("spikebot start (variant %s): cash %.2f, theta %.2f bps / %d ms, latency %d ms, price %.2f-%.2f",
                 C.VARIANT, self.ledger.cash, C.THETA_BPS, C.SIGNAL_WINDOW_MS, C.SIM_LATENCY_MS, C.P_MIN, C.P_MAX)
        self.ledger.event("start", f"cash {self.ledger.cash:.2f}")
        self.ledger.event("config", json.dumps({k: v for k, v in vars(C).items()
                                                if k.isupper() and isinstance(v, (int, float, str))}))
        if self.live is not None:
            self.live.start(asyncio.get_running_loop())
        tasks = [asyncio.create_task(c) for c in (self.markets_loop(), self.binance_loop(), self.settler_loop(),
                                                  self.status_loop())]
        await self.stop.wait()
        for task in tasks + ([self.ws_task] if self.ws_task else []):
            task.cancel()
        self.ledger.record_equity()
        self.ledger.event("stop", f"cash {self.ledger.cash:.2f}")
        log.info("spikebot stopped: cash %.2f", self.ledger.cash)

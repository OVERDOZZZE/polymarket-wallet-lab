"""Cancel latency DURING BTC moves (server, REAL orders that cannot fill): LONDON_PLAN.md before Phase 4.

    .venv/bin/python -m scripts.cancel_stress_probe [minutes]        (from ~/app; default 60)

Keeps one post-only BUY at 0.01 resting on each token of the ACTIVE BTC 5m window (5.37 Up / 5.73 Down shares,
~$0.05 each; only while that token's best bid is 0.20-0.80, from start + 10 s to end - 60 s, so a fill needs a
collapse to 1 c). Cancels:
  spike  the moment Binance USD-M futures moves >= 0.5 bps within 1 s (imb_maker's guard), i.e. when every maker
         on Polymarket is cancelling and the post-mortem saw our cancels answered in a median 112 ms;
  calm   every ~12 s when no spike for 3 s (baseline);
  stop   at end - 60 s.
Re-posts 1 s after a cancel is answered. Per cancel: ack ms (our clock), off-the-book ms (exchange clock, from the
book feed), spike size, and book messages of that market in the 500 ms after the send (congestion). Stops for good
on any fill. Prints timings only, never keys. Output: data/probe/stress-<utc>.json (rewritten every minute).
"""
import asyncio
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from backend.bots.strategies.imb_maker import BinanceGuard  # noqa: E402
from backend.config import BINANCE_FUT_WS, CLOB_MARKET_WS  # noqa: E402

PRICE = 0.01
SIZE = {"up": 5.37, "down": 5.73}
BAND = (0.20, 0.80)
CALM_EVERY_S, QUIET_S, REPOST_S = 12.0, 3.0, 1.0
OUT = Path("data/probe") / time.strftime("stress-%Y%m%d-%H%M%S.json", time.gmtime())


def market(start):
    from backend.polymarket.markets import fetch_market, window_slug
    m = fetch_market(window_slug("BTC", 5, start))
    return {"start": start, "cond": m["conditionId"], "tok": {"up": m["tokens"]["up"], "down": m["tokens"]["down"]}}


class Probe:
    def __init__(self, minutes):
        from backend.trading.fastorders import FastOrders
        from backend.trading.gateway import Gateway
        self.gw = Gateway()
        self.gw.light = True
        self.gw._init()
        if not self.gw.enabled:
            sys.exit(f"gateway: {self.gw.reason}")
        self.fo = FastOrders(self.gw)
        self.end = time.time() + minutes * 60
        self.guard = BinanceGuard(0.5, 1000.0, 1000.0)
        self.win = None                       # current market
        self.bids = {}                        # token -> {price: size}
        self.ev = []                          # (recv ms, exchange ms, token, size at 0.01)
        self.msgs = collections.deque()       # recv ms of book messages (current market)
        self.orders = {}                      # side -> {oid, t_send, state, ...}
        self.rows = []                        # one per cancel
        self.posts = []
        self.last_spike, self.spike_bps = 0.0, 0.0
        self.next_ok = {"up": 0.0, "down": 0.0}
        self.stop_reason = None
        self.bf_n = 0

    # ---------------------------------------------------------------- feeds
    async def binance(self):
        import websockets
        while not self.stop_reason and time.time() < self.end:
            try:
                async with websockets.connect(BINANCE_FUT_WS, ping_interval=15, max_size=None) as ws:
                    while not self.stop_reason and time.time() < self.end:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        now = time.time() * 1000
                        d = json.loads(raw)
                        if "p" not in d:
                            continue
                        self.bf_n += 1
                        p = float(d["p"])
                        side = self.guard.on_tick(now, p)
                        if side:
                            t0, p0 = self.guard.ticks[0]
                            bps = (p / p0 - 1) * 1e4
                            fresh = now - self.last_spike > 1000
                            self.last_spike, self.spike_bps = now, bps
                            if fresh:
                                self.cancel_all("spike", bps)
            except Exception as e:
                print(time.strftime("%H:%M:%S"), "binance:", type(e).__name__, e, flush=True)
                await asyncio.sleep(1)

    async def book(self, w):
        import websockets
        toks = list(w["tok"].values())
        while not self.stop_reason and self.win is w:
            try:
                async with websockets.connect(CLOB_MARKET_WS, ping_interval=15, max_size=None) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    while not self.stop_reason and self.win is w:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        recv = time.time() * 1000
                        if raw == "PONG":
                            continue
                        self.msgs.append(recv)
                        d = json.loads(raw)
                        for e in d if isinstance(d, list) else [d]:
                            ex = float(e.get("timestamp") or 0)
                            if e.get("event_type") == "book":
                                a = e["asset_id"]
                                self.bids[a] = {float(x["price"]): float(x["size"]) for x in e.get("bids", [])}
                                self.ev.append((recv, ex, a, self.bids[a].get(PRICE, 0.0)))
                            elif e.get("event_type") == "price_change":
                                for c in e.get("price_changes", []):
                                    if c.get("side") != "BUY":
                                        continue
                                    a, px, sz = c["asset_id"], float(c["price"]), float(c["size"])
                                    b = self.bids.setdefault(a, {})
                                    if sz > 0:
                                        b[px] = sz
                                    else:
                                        b.pop(px, None)
                                    if abs(px - PRICE) < 1e-9:
                                        self.ev.append((recv, ex, a, sz))
            except Exception as e:
                print(time.strftime("%H:%M:%S"), "book:", type(e).__name__, e, flush=True)
                await asyncio.sleep(1)

    # ---------------------------------------------------------------- orders
    def best_bid(self, tok):
        b = self.bids.get(tok)
        return max((p for p, s in b.items() if s > 0), default=None) if b else None

    def post(self, side):
        w = self.win
        tok = w["tok"][side]
        signed, oid, _ = self.fo.sign(tok, PRICE, SIZE[side])
        o = {"side": side, "oid": oid, "tok": tok, "t_send": time.time() * 1000, "state": "sending", "slug": w["start"]}
        self.orders[side] = o
        f = self.fo.post(signed, oid)
        f.add_done_callback(lambda f, o=o: self.loop.call_soon_threadsafe(self.posted, o, f.result()))

    def posted(self, o, r):
        o["post"] = r
        self.posts.append({"side": o["side"], "ok": r["ok"], "ms": r["post_ms"], "err": r["error"]})
        if not r["ok"]:
            if self.orders.get(o["side"]) is o:
                del self.orders[o["side"]]
            self.next_ok[o["side"]] = time.time() + 5
            return
        if o["state"] == "sending":
            o["state"] = "live"

    def cancel_all(self, kind, bps=0.0):
        for o in list(self.orders.values()):
            if o["state"] in ("sending", "live"):
                self.cancel(o, kind, bps)

    def cancel(self, o, kind, bps):
        now = time.time() * 1000
        o.update(state="cancelling", kind=kind, bps=round(bps, 2), t_csend=now)
        f = self.fo.cancel([o["oid"]])
        f.add_done_callback(lambda f, o=o: self.loop.call_soon_threadsafe(self.cancelled, o, f.result()))

    def cancelled(self, o, r):
        n500 = sum(1 for t in self.msgs if o["t_csend"] <= t <= o["t_csend"] + 500)
        row = {k: o.get(k) for k in ("side", "oid", "tok", "slug", "kind", "bps", "t_send", "t_csend")}
        row.update(ack_ms=r["ms"], ok=o["oid"] in r["canceled"], not_canceled=r["not_canceled"],
                   err=r["error"], book_msgs_500ms=n500, live_s=round((o["t_csend"] - o["t_send"]) / 1000, 1),
                   post_ok=(o.get("post") or {}).get("ok"))
        self.rows.append(row)
        if self.orders.get(o["side"]) is o:
            del self.orders[o["side"]]
        self.next_ok[o["side"]] = time.time() + REPOST_S
        if not row["ok"] and o.get("post", {}).get("ok"):
            asyncio.ensure_future(self.check_fill(o))

    async def check_fill(self, o):
        try:
            x = await asyncio.to_thread(self.gw.client.get_order, order_id=o["oid"])
            matched = float(x.size_matched or 0)
        except Exception as e:
            print("check_fill error", type(e).__name__, str(e)[:120], flush=True)
            matched = 0.0
        if matched > 0:
            self.stop_reason = f"FILL {matched} sh on {o['side']} @ {PRICE}"
            print("STOP:", self.stop_reason, flush=True)

    # ---------------------------------------------------------------- control
    async def run(self):
        self.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(self.binance())]
        last_save = time.time()
        try:
            while not self.stop_reason and time.time() < self.end:
                now = time.time()
                start = int(now) // 300 * 300
                if self.win is None or self.win["start"] != start:
                    self.cancel_all("stop")
                    try:
                        self.win = await asyncio.to_thread(market, start)
                    except Exception as e:
                        print("market error", e, flush=True)
                        await asyncio.sleep(2)
                        continue
                    self.bids, self.msgs = {}, collections.deque()
                    tasks.append(asyncio.create_task(self.book(self.win)))
                    print(time.strftime("%H:%M:%S"), "window", start, "| cancels so far", len(self.rows),
                          "spike", sum(r["kind"] == "spike" for r in self.rows), flush=True)
                while self.msgs and self.msgs[0] < now * 1000 - 5000:
                    self.msgs.popleft()
                active = start + 10 <= now <= start + 240
                if not active:
                    self.cancel_all("stop")
                else:
                    for side in ("up", "down"):
                        o = self.orders.get(side)
                        if o is None and now >= self.next_ok[side]:
                            bb = self.best_bid(self.win["tok"][side])
                            if bb is not None and BAND[0] <= bb <= BAND[1]:
                                self.post(side)
                        elif o is not None and o["state"] == "live" and now * 1000 - o["t_send"] > CALM_EVERY_S * 1000 \
                                and now * 1000 - self.last_spike > QUIET_S * 1000:
                            self.cancel(o, "calm", 0.0)
                if now - last_save > 60:
                    last_save = now
                    self.save()
                await asyncio.sleep(0.05)
        finally:
            self.cancel_all("stop")
            await asyncio.sleep(2.0)
            try:                                  # sweep: any of OUR probe orders still open in the last market
                ids = {r["oid"] for r in self.rows} | {o["oid"] for o in self.orders.values()}
                left = [x["id"] for x in self.gw.open_orders_in(self.win["cond"]) if x["id"] in ids]
                if left:
                    print("sweep", self.gw.cancel_batch(left), flush=True)
            except Exception as e:
                print("sweep error", e, flush=True)
            for t in tasks:
                t.cancel()
            self.save()
            self.summary()

    def save(self):
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"rows": self.rows, "posts": self.posts, "ev": self.ev, "stop": self.stop_reason,
                                   "binance_msgs": self.bf_n}, default=str))

    def summary(self):
        offs = off_book(self.rows, self.ev)
        q = lambda xs: (f"n {len(xs)} p10 {sorted(xs)[len(xs) // 10]:.0f} p50 {sorted(xs)[len(xs) // 2]:.0f} "
                        f"p90 {sorted(xs)[int(len(xs) * .9)]:.0f}" if xs else "-")
        print(f"\nsaved {OUT}; stop: {self.stop_reason or 'time'}; posts {len(self.posts)} "
              f"(failed {sum(not p['ok'] for p in self.posts)})")
        for kind in ("calm", "spike", "stop"):
            R = [r for r in self.rows if r["kind"] == kind and r["post_ok"]]
            print(f"{kind:6s} cancels: ack {q([r['ack_ms'] for r in R])} | off book "
                  f"{q([offs[r['oid']] for r in R if r['oid'] in offs])}")
        big = [r for r in self.rows if r["kind"] == "spike" and r["post_ok"] and abs(r["bps"] or 0) >= 1.0]
        print(f"spike >= 1 bps: ack {q([r['ack_ms'] for r in big])} | off book "
              f"{q([offs[r['oid']] for r in big if r['oid'] in offs])}")


def off_book(rows, ev):
    """oid -> exchange ms from cancel send to the book event where the level shrank by our size."""
    by_tok = collections.defaultdict(list)
    for recv, ex, tok, sz in ev:
        by_tok[tok].append((ex, sz))
    out = {}
    for r in rows:
        size = SIZE[r["side"]]
        evs = by_tok.get(r["tok"], [])
        prev = None
        for ex, sz in evs:
            if prev is not None and ex >= r["t_csend"] - 5 and abs((prev - sz) - size) < 1e-6:
                if ex - r["t_csend"] < 3000:
                    out[r["oid"]] = ex - r["t_csend"]
                break
            prev = sz
    return out


if __name__ == "__main__":
    m = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    asyncio.run(Probe(m).run())

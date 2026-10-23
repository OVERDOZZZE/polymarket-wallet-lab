"""Maker order timing from this server: post-only order and cancel, N rounds (default 20).

Each round posts a post-only BUY of 5 Up shares @ $0.01 on the NEXT BTC 5m window (cost if it ever filled: $0.05),
waits until the order shows on the public book feed, cancels it, and waits until it is gone from the feed.
Timed per round:
  sign      - building + signing the order locally
  post      - post_order round trip (send -> response)
  post_seen - send -> our +5 shares visible on the market websocket (engine accepted it + feed delay)
  cancel    - cancel_order round trip
  cancel_seen - cancel sent -> our -5 shares visible on the feed (the cancel has taken effect at the engine)
  feed      - feed delay of those book events (their exchange timestamp -> received here, NTP-corrected)
Prints only timings and order status - never keys or wallet data.
Run on the server from ~/app:  .venv/bin/python scripts/maker_latency_test.py [rounds]
"""
import asyncio
import json
import sys
import threading
import time

sys.path.insert(0, ".")
from backend.config import CLOB_MARKET_WS  # noqa: E402
from backend.polymarket.markets import fetch_market  # noqa: E402
from backend.trading.gateway import Gateway, _api_error  # noqa: E402
from backend.util import ntp_offset_ms  # noqa: E402

PRICE, SIZE = 0.01, 5.0
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20

events = []              # (perf_counter at receive, wall ms at receive, exchange ts ms, old size, new size)
level = {"size": None}
lock = threading.Lock()


def book_reader(tok, ready):
    import websockets

    async def run():
        async with websockets.connect(CLOB_MARKET_WS, ping_interval=None, max_size=None, compression="deflate") as ws:
            await ws.send(json.dumps({"assets_ids": [tok], "type": "market"}))
            last_ping = time.time()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    raw = None
                if time.time() - last_ping > 9:
                    await ws.send("PING")
                    last_ping = time.time()
                if not raw or raw == "PONG":
                    continue
                pc, wall = time.perf_counter(), time.time() * 1000
                d = json.loads(raw)
                for e in d if isinstance(d, list) else [d]:
                    if e.get("event_type") == "book" and e.get("asset_id") == tok:
                        with lock:
                            level["size"] = sum(float(x["size"]) for x in e.get("bids", [])
                                                if abs(float(x["price"]) - PRICE) < 1e-9)
                        ready.set()
                    elif e.get("event_type") == "price_change":
                        for c in e.get("price_changes", []):
                            if c.get("asset_id") != tok or c.get("side") != "BUY" or abs(float(c["price"]) - PRICE) > 1e-9:
                                continue
                            with lock:
                                old, new = level["size"] or 0.0, float(c["size"])
                                level["size"] = new
                                events.append((pc, wall, float(e.get("timestamp") or 0), old, new))
    asyncio.run(run())


def wait_delta(t_send, delta, timeout=3.0):
    """First feed event after t_send that changed our level by `delta` shares: (its receive perf time, event)."""
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        with lock:
            for ev in events:
                if ev[0] >= t_send and abs((ev[4] - ev[3]) - delta) < 1e-6:
                    return ev
        time.sleep(0.001)
    return None


def stats(name, xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return f"{name:12s} no data"
    return (f"{name:12s} n={len(xs):2d}  min {xs[0]:6.1f}  p50 {xs[len(xs) // 2]:6.1f}  "
            f"p90 {xs[min(len(xs) - 1, int(len(xs) * .9))]:6.1f}  max {xs[-1]:6.1f} ms")


def main():
    g = Gateway()
    g._init()
    if not g.enabled:
        sys.exit(f"trading client not available: {g.reason}")
    off = ntp_offset_ms() or 0.0
    slug = f"btc-updown-5m-{int(time.time()) // 300 * 300 + 300}"
    tok = fetch_market(slug)["tokens"]["up"]
    print(f"market {slug} | {ROUNDS} rounds of post-only BUY {SIZE:g} Up @ {PRICE} (${PRICE * SIZE:.2f}) + cancel"
          f" | clock offset {off:+.1f} ms")
    ready = threading.Event()
    threading.Thread(target=book_reader, args=(tok, ready), daemon=True).start()
    if not ready.wait(10):
        sys.exit("no book snapshot from the market websocket")
    # the first order per token is slow to sign (SDK caches tick size / fee data): warm it up, unsent
    g.client.create_limit_order(token_id=tok, price=str(PRICE), size=f"{SIZE:.2f}", side="BUY", post_only=True)
    rows = []
    for i in range(ROUNDS):
        r = {"sign": None, "post": None, "post_seen": None, "cancel": None, "cancel_seen": None, "feed": []}
        t0 = time.perf_counter()
        order = g.client.create_limit_order(token_id=tok, price=str(PRICE), size=f"{SIZE:.2f}", side="BUY",
                                            post_only=True)
        t1 = time.perf_counter()
        r["sign"] = (t1 - t0) * 1000
        try:
            resp = g.client.post_order(order)
            d = resp.model_dump(mode="json")
            oid = d.get("order_id")
            ok = resp.ok and oid
        except Exception as e:
            print(f"round {i + 1}: post REJECTED: {_api_error(e)}")
            break
        t2 = time.perf_counter()
        r["post"] = (t2 - t1) * 1000
        if not ok:
            print(f"round {i + 1}: post not ok: status={d.get('status')} message={getattr(resp, 'message', None)!r}")
            break
        ev = wait_delta(t1, SIZE)
        if ev:
            r["post_seen"] = (ev[0] - t1) * 1000
            if ev[2]:
                r["feed"].append(ev[1] + off - ev[2])
        time.sleep(0.3)
        t3 = time.perf_counter()
        try:
            c = g.client.cancel_order(order_id=oid).model_dump(mode="json")
        except Exception as e:
            print(f"round {i + 1}: cancel FAILED: {_api_error(e)}")
            break
        t4 = time.perf_counter()
        r["cancel"] = (t4 - t3) * 1000
        if not c.get("canceled"):
            print(f"round {i + 1}: cancel not confirmed: not_canceled={c.get('not_canceled')}")
        ev = wait_delta(t3, -SIZE)
        if ev:
            r["cancel_seen"] = (ev[0] - t3) * 1000
            if ev[2]:
                r["feed"].append(ev[1] + off - ev[2])
        rows.append(r)
        f = lambda x: "  -  " if x is None else f"{x:5.0f}"  # noqa: E731
        print(f"round {i + 1:2d}: sign {f(r['sign'])}  post {f(r['post'])}  seen {f(r['post_seen'])}  |  "
              f"cancel {f(r['cancel'])}  gone {f(r['cancel_seen'])} ms")
        time.sleep(0.7)
    still = [o for o in g.client.list_open_orders().iter_items()
             if abs(float(o.model_dump(mode="json").get("price") or 0) - PRICE) < 1e-9
             and o.model_dump(mode="json").get("asset_id") == tok]
    print(f"open test orders left: {len(still)}")
    print()
    for k in ("sign", "post", "post_seen", "cancel", "cancel_seen"):
        print(stats(k, [r[k] for r in rows]))
    print(stats("feed", [x for r in rows for x in r["feed"]]))


if __name__ == "__main__":
    main()

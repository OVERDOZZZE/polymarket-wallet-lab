"""Order speed probe (server, real orders that cannot fill): how fast can we post and cancel through
backend/trading/fastorders.py, measured on our clock and on the exchange's book feed.

    .venv/bin/python -m scripts.order_speed_probe [n] [--site london]       # n orders per test (default 30)
    .venv/bin/python -m scripts.order_speed_probe --summarize data/probe/<utc>.json [--site london]   # no orders

Every order is a post-only BUY of 5.37 Up shares at 0.02 (~$0.11) on the NEXT 5m window (its book sits near
0.50 before it opens, so nothing can fill), cancelled within the test. Tests:
  seq        post, wait for the ack, then cancel, wait (one at a time)
  pipelined  post and, without waiting, cancel the same order by its locally computed id
  burst5     5 posts at once, then their 5 cancels at once
Pacing keeps us far below the account's rate limits (40 orders/s): another bot shares the wallet.
Only our own order ids are ever cancelled; a final sweep cancels any of ours still open (size 5.37 at 0.02).
Output: data/probe/<utc>.json and a summary; with --site also data/latency/orders-<site>-<utc>.json (post / cancel
ack, on / off the book) for scripts/latency_build.py.
"""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

PRICE, SIZE = 0.02, 5.37


def book_listener(tokens, out, stop):
    import websockets

    from backend.config import CLOB_MARKET_WS

    async def run():
        async with websockets.connect(CLOB_MARKET_WS, ping_interval=15, max_size=None) as ws:
            await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                recv = time.time() * 1000
                d = json.loads(raw)
                for e in d if isinstance(d, list) else [d]:
                    if e.get("event_type") == "price_change":
                        for c in e.get("price_changes", []):
                            if c.get("side") == "BUY" and abs(float(c["price"]) - PRICE) < 1e-9:
                                out.append((recv, float(e.get("timestamp") or 0), c.get("asset_id"), float(c["size"])))
                    elif e.get("event_type") == "book":
                        for x in e.get("bids", []):
                            if abs(float(x["price"]) - PRICE) < 1e-9:
                                out.append((recv, float(e.get("timestamp") or 0), e.get("asset_id"), float(x["size"])))
    asyncio.run(run())


def main():
    n = next((int(a) for a in sys.argv[1:] if a.isdigit()), 30)
    from backend.polymarket.markets import fetch_market, window_slug
    from backend.trading.fastorders import FastOrders
    from backend.trading.gateway import Gateway
    gw = Gateway()
    gw.light = True
    gw._init()
    if not gw.enabled:
        sys.exit(f"gateway: {gw.reason}")
    now = time.time()
    start = int(now) // 300 * 300 + 300
    if start - now < 90:
        start += 300
    m = fetch_market(window_slug("BTC", 5, start))
    tok = m["tokens"]["up"]
    print("window", m["slug"], "starts in", round(start - now), "s; token", tok[:10])
    fo = FastOrders(gw)
    book, stop = [], threading.Event()
    threading.Thread(target=book_listener, args=([tok], book, stop), daemon=True).start()
    time.sleep(2.0)
    fo.sign(tok, PRICE, SIZE)                     # warm: market metadata, exchange address
    rows = []

    def one(test, cancel_now):
        signed, oid, sign_ms = fo.sign(tok, PRICE, SIZE)
        t_send = time.time() * 1000
        fp = fo.post(signed, oid)
        fc, t_csend = None, None
        if cancel_now:
            t_csend = time.time() * 1000
            fc = fo.cancel([oid])
        return {"test": test, "oid": oid, "sign_ms": sign_ms, "t_send": t_send, "fp": fp, "fc": fc, "t_csend": t_csend}

    def finish(r):
        p = r.pop("fp").result()
        r.update(post=p)
        if r["fc"] is None:
            r["t_csend"] = time.time() * 1000
            r["fc"] = fo.cancel([p["order_id"]])
        r["cancel"] = r.pop("fc").result()
        rows.append(r)

    try:
        for _ in range(n):                        # seq
            r = one("seq", False)
            finish(r)
            time.sleep(0.3)
        for _ in range(n):                        # pipelined
            r = one("pipelined", True)
            finish(r)
            time.sleep(0.3)
        for _ in range(max(n // 5, 1)):           # burst5
            rs = [one("burst5", False) for _ in range(5)]
            ps = [r.pop("fp").result() for r in rs]
            t_c = time.time() * 1000
            cs = [fo.cancel([p["order_id"]]) for p in ps]
            for r, p, c in zip(rs, ps, cs):
                r.update(post=p, t_csend=t_c, cancel=c.result())
                r.pop("fc", None)
                rows.append(r)
            time.sleep(1.0)
    finally:
        time.sleep(1.5)
        stop.set()
        try:                                      # sweep: any of OUR probe orders still open
            left = [o for o in gw.open_orders_in(m["conditionId"])
                    if abs(float(o.get("price") or 0) - PRICE) < 1e-9 and abs(float(o.get("original_size") or 0) - SIZE) < 1e-6]
            ids = [o["id"] for o in left if any(r["post"]["order_id"] == o["id"] for r in rows if "post" in r)]
            if ids:
                print("sweep cancels", len(ids), fo.cancel(ids).result())
        except Exception as e:
            print("sweep error", e)
    out = Path("data/probe")
    out.mkdir(parents=True, exist_ok=True)
    f = out / time.strftime("%Y%m%d-%H%M%S.json", time.gmtime())
    f.write_text(json.dumps({"slug": m["slug"], "rows": rows, "book": book, "verified": fo.verified,
                             "mismatch": fo.mismatch}, default=str))
    print("saved", f, "| local id verified:", fo.verified, "mismatches:", fo.mismatch)
    summarize(rows, book)
    if "--site" in sys.argv:
        write_site(rows, book, sys.argv[sys.argv.index("--site") + 1], str(f))


def book_times(rows, book):
    """Exchange-clock delays of the ACCEPTED orders: send -> on the book, cancel sent -> off the book, and the feed
    delay (receive - exchange time) of those book events. A book change of k x SIZE counts as k orders (bursts)."""
    ev, prev = [], None
    for recv, ex, _a, sz in book:
        if prev is not None:
            k = (sz - prev) / SIZE
            if abs(k) >= 0.99 and abs(k - round(k)) < 0.01:
                ev += [(ex, recv, 1 if k > 0 else -1)] * int(abs(round(k)))
        prev = sz
    ok = [r for r in rows if r["post"]["ok"]]

    def match(ts, evs):
        d, j = [], 0
        for t in sorted(ts):
            while j < len(evs) and evs[j][0] < t - 5:
                j += 1
            if j < len(evs) and evs[j][0] - t < 1500:
                d.append((evs[j][0] - t, evs[j][1] - evs[j][0]))
                j += 1
        return d
    on = match([r["t_send"] for r in ok], sorted(e for e in ev if e[2] == 1))
    off = match([r["t_csend"] for r in ok], sorted(e for e in ev if e[2] == -1))
    return [x for x, _ in on], [x for x, _ in off], [y for _, y in on + off]


def write_site(rows, book, site, src):
    """data/latency/orders-<site>-<utc>.json: this site's order timings for scripts/latency_build.py."""
    from backend.config import LATENCY_DIR
    from scripts.latency_probe import summary
    seq = [r for r in rows if r["test"] in ("seq", "burst5")]          # pipelined posts mostly lose to their cancel
    on, off, feed = book_times(rows, book)
    res = {"site": site, "source": src, "orders": len(rows), "accepted": sum(r["post"]["ok"] for r in rows),
           "post_ack": summary([r["post"]["post_ms"] for r in seq if r["post"]["ok"]]),
           "cancel_ack": summary([r["cancel"]["ms"] for r in seq if r["cancel"]["canceled"]]),
           "on_book": summary(on), "off_book": summary(off), "book_feed": summary(feed)}
    LATENCY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = Path(src).stem[-15:] if Path(src).stem[-15:].replace("-", "").isdigit() else time.strftime(
        "%Y%m%d-%H%M%S", time.gmtime())
    f = LATENCY_DIR / f"orders-{site}-{stamp}.json"
    f.write_text(json.dumps(res, indent=1))
    print("saved", f)


def summarize(rows, book):
    import statistics as st
    q = lambda xs: (f"p10 {sorted(xs)[len(xs) // 10]:.0f} p50 {st.median(xs):.0f} p90 {sorted(xs)[int(len(xs) * .9)]:.0f}"
                    if xs else "-")
    for test in ("seq", "pipelined", "burst5"):
        R = [r for r in rows if r["test"] == test]
        ok = [r for r in R if r["post"]["ok"]]
        canc = [r for r in R if r["cancel"]["canceled"]]
        print(f"\n{test}: {len(R)} orders, {len(ok)} accepted, {len(canc)} cancelled; errors:",
              {r["post"]["error"] for r in R if not r["post"]["ok"]} or "-")
        print("  sign ms   ", q([r["sign_ms"] for r in R]))
        print("  post ms   ", q([r["post"]["post_ms"] for r in R]))
        print("  cancel ms ", q([r["cancel"]["ms"] for r in R]))
        nc = [list(r["cancel"]["not_canceled"].values())[0] for r in R if r["cancel"]["not_canceled"]]
        if nc:
            print("  not cancelled:", nc[:3])
    on, off, feed = book_times(rows, book)       # accepted orders only (pipelined posts often lose to their cancel)
    print(f"\nbook feed: {len(on)} appearances, {len(off)} removals of our {SIZE} orders at {PRICE}")
    print("  send -> on the book (exchange ms)    ", q(on))
    print("  cancel sent -> off the book (exch ms)", q(off))
    print("  book feed delay (receive - exchange) ", q(feed))


if __name__ == "__main__":
    if "--summarize" in sys.argv:                 # re-read a saved run, no orders: --summarize <json> [--site s]
        src = sys.argv[sys.argv.index("--summarize") + 1]
        d = json.loads(Path(src).read_text())
        summarize(d["rows"], d["book"])
        if "--site" in sys.argv:
            write_site(d["rows"], d["book"], sys.argv[sys.argv.index("--site") + 1], src)
    else:
        main()

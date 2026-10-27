"""Where does an order's time go? (server, real post-only orders that cannot fill, run by the user's OK)

    .venv/bin/python -m scripts.delay_probe [--pcap /tmp/probe.pcap]

Every order is a post-only BUY of 5.37 Up shares at 0.02 (~$0.11), cancelled within the test. One process, no bot
engine in it. Per request it records, on this machine's clock (NTP-synced, same clock as tcpdump):
  t_req   httpx request hook: the request is about to be written to the socket
  t_resp  httpx response hook: the response headers have been read
  the response's Poly-RateLimit-* / cf-ray / server-timing headers and the connection's local port,
and from the CLOB market feed, the exchange timestamp at which a 5.37 order appeared / left at 0.02.
So for one order: our send -> on the book (the engine has it) -> response back to us. With --pcap (tcpdump running
on the same box) the response's first packet on the wire is found too, outside Python.
Tests (the active window only while its Up best bid is 0.15-0.85 and >= 60 s are left; 0.02 cannot fill there):
  active_seq    20 x: post, wait for the response, cancel, wait          (one request at a time)
  next_seq      20 x the same on the next window (not open yet: the 10-26 probe's setting)
  active_burst  3 x: 10 posts at once, then their 10 cancels at once
  active_load   30 s of the bot's pattern: ~3 posts/s, each cancelled 150 ms later without waiting for its response
  next_pipe / active_pipe  15 x: post, and 5 ms later (its response not back yet) cancel it, on a SECOND client
                (own TCP connection), so each response's packets are told apart on the wire
  active_pipe_burst  3 x: 7 posts ~7 ms apart, each cancelled 5 ms after it (imb-maker-10's 1.04 s burst)
  --tests a,b,... picks tests (default: all of the above in this order).
Output: data/probe/delay-<utc>.json and a summary.
"""
import collections
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.order_speed_probe import PRICE, SIZE

HDRS = ("poly-ratelimit-remaining", "poly-ratelimit-reset", "poly-ratelimit-tier", "poly-ratelimit-warning",
        "retry-after", "cf-ray", "server-timing", "x-response-time", "date")


class Hooks:
    """httpx event hooks on the SDK's L2 CLOB client: request / response times by order salt or cancelled id."""

    def __init__(self):
        self.req, self.resp, self.lock = {}, {}, threading.Lock()

    @staticmethod
    def _keys(request):
        try:
            body = json.loads(request.content or b"null")
        except ValueError:
            return []
        if isinstance(body, dict) and isinstance(body.get("order"), dict):
            return [("post", str(body["order"].get("salt")))]
        ids = body if isinstance(body, list) else (body or {}).get("orderIDs") or (body or {}).get("orderID") or []
        return [("cancel", str(i)) for i in ([ids] if isinstance(ids, str) else ids)]

    def on_request(self, request):
        t = time.time() * 1000
        with self.lock:
            for k in self._keys(request):
                self.req[k] = t

    def on_response(self, response):
        t = time.time() * 1000
        ns = response.extensions.get("network_stream")
        port = None
        try:
            port = ns.get_extra_info("client_addr")[1]
        except Exception:
            pass
        info = {"t": t, "status": response.status_code, "http": response.http_version, "port": port,
                "h": {k: response.headers.get(k) for k in HDRS if response.headers.get(k) is not None}}
        with self.lock:
            for k in self._keys(response.request):
                self.resp[k] = info


def book_listener(token, out, best, stop):
    import asyncio

    import websockets

    from backend.config import CLOB_MARKET_WS

    async def run():
        async with websockets.connect(CLOB_MARKET_WS, ping_interval=15, max_size=None) as ws:
            await ws.send(json.dumps({"assets_ids": [token], "type": "market"}))
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                recv = time.time() * 1000
                d = json.loads(raw)
                for e in d if isinstance(d, list) else [d]:
                    ex = float(e.get("timestamp") or 0)
                    if e.get("event_type") == "price_change":
                        for c in e.get("price_changes", []):
                            if c.get("asset_id") == token and c.get("best_bid"):
                                best["bid"] = float(c["best_bid"])
                            if c.get("side") == "BUY" and abs(float(c["price"]) - PRICE) < 1e-9:
                                out.append((recv, ex, float(c["size"])))
                    elif e.get("event_type") == "book":
                        bids = e.get("bids", [])
                        if bids:
                            best["bid"] = max(float(x["price"]) for x in bids)
                        out.append((recv, ex, sum(float(x["size"]) for x in bids if abs(float(x["price"]) - PRICE) < 1e-9)))
    asyncio.run(run())


def main():
    pcap = sys.argv[sys.argv.index("--pcap") + 1] if "--pcap" in sys.argv else None
    tests = (sys.argv[sys.argv.index("--tests") + 1].split(",") if "--tests" in sys.argv else
             ["active_seq", "next_seq", "active_burst", "active_load", "next_pipe", "active_pipe", "active_pipe_burst"])
    from backend.polymarket.markets import fetch_market, window_slug
    from backend.trading.fastorders import FastOrders
    from backend.trading.gateway import Gateway
    hooks = Hooks()
    gws = []
    for _ in range(2):                               # [0] posts (and the seq tests' cancels), [1] the pipe tests' cancels
        gw = Gateway()
        gw.light = True
        gw._init()
        if not gw.enabled:
            sys.exit(f"gateway: {gw.reason}")
        gw.client._ctx.secure_clob._client.event_hooks = {"request": [hooks.on_request], "response": [hooks.on_response]}
        gws.append(gw)
    gw = gws[0]
    fo, fo2 = FastOrders(gw), FastOrders(gws[1])
    burst = ThreadPoolExecutor(24)
    now = time.time()
    cur = int(now) // 300 * 300
    if cur + 300 - now < 150:                        # need ~90 s of the active window with >= 60 s left
        print("waiting for the next window to open ...")
        time.sleep(cur + 300 - now + 15)
        cur += 300
    markets = {"active": fetch_market(window_slug("BTC", 5, cur)), "next": fetch_market(window_slug("BTC", 5, cur + 300))}
    for name, m in markets.items():
        try:
            info = gw.client._ctx.secure_clob._client.get(f"/clob-markets/{m['conditionId']}").json()
            flags = {k: v for k, v in info.items() if not isinstance(v, (dict, list))} if isinstance(info, dict) else info
        except Exception as e:
            flags = f"{type(e).__name__}: {e}"
        print(name, m["slug"], "clob-markets:", json.dumps(flags, default=str)[:400])
    rows, books = [], {}

    def one(test, m, wait=True, cancel_after_ms=None, cfo=None):
        tok = m["tokens"]["up"]
        signed, oid, sign_ms = fo.sign(tok, PRICE, SIZE)
        r = {"test": test, "slug": m["slug"], "oid": oid, "salt": str(signed.salt), "sign_ms": sign_ms,
             "t_call": time.time() * 1000}
        fp = fo.post(signed, oid)
        if cancel_after_ms is not None:
            time.sleep(cancel_after_ms / 1000)
            r["t_ccall"] = time.time() * 1000
            r["fc"] = (cfo or fo).cancel([oid])
        if wait:
            r["post"] = fp.result()
            if "fc" not in r:
                r["t_ccall"] = time.time() * 1000
                r["cancel"] = fo.cancel([oid]).result()
            else:
                r["cancel"] = r.pop("fc").result()
        else:
            r["fp"] = fp
        rows.append(r)
        return r

    def finish(r):
        if "fp" in r:
            r["post"] = r.pop("fp").result()
        if "fc" in r:
            r["cancel"] = r.pop("fc").result()

    def safe(m):
        b = books[m["slug"]][1].get("bid")
        return b is not None and 0.15 <= b <= 0.85 and m["start"] + 300 - time.time() >= 60

    for name, m in markets.items():
        m["start"] = int(m["slug"].rsplit("-", 1)[1])
        out, best, stop = [], {}, threading.Event()
        books[m["slug"]] = (out, best, stop)
        threading.Thread(target=book_listener, args=(m["tokens"]["up"], out, best, stop), daemon=True).start()
    time.sleep(2.5)
    fo.sign(markets["active"]["tokens"]["up"], PRICE, SIZE)          # warm metadata for both tokens
    fo.sign(markets["next"]["tokens"]["up"], PRICE, SIZE)
    A, N = markets["active"], markets["next"]
    print("active Up best bid", books[A["slug"]][1].get("bid"), "| t_left", round(A["start"] + 300 - time.time()), "s")
    try:
        for test, m in (("active_seq", A), ("next_seq", N)):
            for _ in range(20 if test in tests else 0):
                if m is A and not safe(A):
                    print("active window no longer safe: stop", test)
                    break
                one(test, m)
                time.sleep(0.5)
        for _ in range(3 if "active_burst" in tests else 0):
            if not safe(A):
                break
            rs = [burst.submit(one, "active_burst", A, False) for _ in range(10)]
            rs = [f.result() for f in rs]
            ps = [r.pop("fp").result() for r in rs]
            for r, p in zip(rs, ps):
                r["post"], r["t_ccall"] = p, time.time() * 1000
            cs = [fo.cancel([r["oid"]]) for r in rs]
            for r, c in zip(rs, cs):
                r["cancel"] = c.result()
            time.sleep(2.0)
        t_end, pend = time.time() + (30 if "active_load" in tests else 0), []
        while time.time() < t_end and safe(A):
            pend.append(burst.submit(one, "active_load", A, False, 150))
            time.sleep(random.expovariate(3.0))
        for f in pend:
            finish(f.result())
        for test, m in (("next_pipe", N), ("active_pipe", A)):
            for _ in range(15 if test in tests else 0):
                if m is A and not safe(A):
                    print("active window no longer safe: stop", test)
                    break
                finish(one(test, m, False, 5, fo2))
                time.sleep(0.8)
        for _ in range(3 if "active_pipe_burst" in tests else 0):
            if not safe(A):
                break
            pend = []
            for _ in range(7):
                pend.append(burst.submit(one, "active_pipe_burst", A, False, 5, fo2))
                time.sleep(0.007)
            for f in pend:
                finish(f.result())
            time.sleep(2.0)
    finally:
        time.sleep(2.0)
        for out, best, stop in books.values():
            stop.set()
        for m in markets.values():                   # sweep: any of OUR probe orders still open
            try:
                mine = {r["oid"] for r in rows}
                left = [o["id"] for o in gw.open_orders_in(m["conditionId"]) if o["id"] in mine]
                if left:
                    print("sweep cancels", len(left), fo.cancel(left).result())
            except Exception as e:
                print("sweep error", e)
    for r in rows:
        r["req"] = hooks.req.get(("post", r["salt"]))
        r["resp"] = hooks.resp.get(("post", r["salt"]))
        r["creq"] = hooks.req.get(("cancel", r["oid"]))
        r["cresp"] = hooks.resp.get(("cancel", r["oid"]))
    match_book(rows, books)
    if pcap:
        match_pcap(rows, pcap)
    out = Path("data/probe")
    out.mkdir(parents=True, exist_ok=True)
    f = out / time.strftime("delay-%Y%m%d-%H%M%S.json", time.gmtime())
    f.write_text(json.dumps({"markets": {k: v["slug"] for k, v in markets.items()}, "rows": rows,
                             "books": {k: v[0] for k, v in books.items()}}, default=str))
    print("saved", f)
    summarize(rows)


def match_book(rows, books):
    """Our order's appearance / removal at 0.02: book size deltas that are multiples of SIZE, given in time order to
    the earliest unmatched sends / cancels of that window (exchange timestamps)."""
    for slug, (out, _b, _s) in books.items():
        adds, rems, prev = [], [], None
        for recv, ex, sz in out:
            if prev is not None:
                k = round((sz - prev) / SIZE)
                if k and abs((sz - prev) - k * SIZE) < 1e-4:
                    (adds if k > 0 else rems).extend([(ex, recv)] * abs(k))
            prev = sz
        R = sorted((r for r in rows if r["slug"] == slug and r.get("req")), key=lambda r: r["req"])
        for key, evs, t0 in (("on_book", adds, "req"), ("off_book", rems, "creq")):
            j = 0
            for r in sorted((r for r in R if r.get(t0)), key=lambda r: r[t0]):
                while j < len(evs) and evs[j][0] < r[t0] - 50:
                    j += 1
                if j < len(evs) and evs[j][0] - r[t0] < 15000:
                    r[key] = evs[j]
                    j += 1


def match_pcap(rows, path):
    """First inbound TLS payload on the request's connection after its request hook (one-at-a-time tests only: with
    several requests in flight on one connection the packets cannot be told apart)."""
    import subprocess
    txt = subprocess.run(["tcpdump", "-r", path, "-n", "-tt", "-q"], capture_output=True, text=True).stdout
    pk = collections.defaultdict(list)                # local port -> [(t_ms, direction, length)]
    for line in txt.splitlines():
        p = line.split()
        try:
            t, src, dst, ln = float(p[0]) * 1000, p[2], p[4].rstrip(":"), int(p[-1])
        except (ValueError, IndexError):
            continue
        if ln <= 0:
            continue
        if src.endswith(".443"):
            pk[int(dst.rsplit(".", 1)[1])].append((t, "in", ln))
        else:
            pk[int(src.rsplit(".", 1)[1])].append((t, "out", ln))
    for r in rows:
        if not r["test"].endswith(("_seq", "_pipe")):     # one post (and one cancel) in flight per connection
            continue
        for kind, rq, rs in (("p", "req", "resp"), ("c", "creq", "cresp")):
            if not r.get(rq) or not r.get(rs) or not r[rs].get("port"):
                continue
            seq = pk.get(r[rs]["port"], [])
            out = next((t for t, d, _ in seq if d == "out" and t >= r[rq] - 1), None)
            inn = next((t for t, d, _ in seq if d == "in" and out is not None and t > out), None)
            r[f"{kind}_wire_out"], r[f"{kind}_wire_in"] = out, inn


def summarize(rows):
    def q(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return "-"
        return f"n={len(xs):3} p10 {xs[len(xs) // 10]:6.0f} p50 {xs[len(xs) // 2]:6.0f} p90 {xs[int(len(xs) * .9)]:6.0f} max {xs[-1]:6.0f}"

    for test in ("active_seq", "next_seq", "active_burst", "active_load", "next_pipe", "active_pipe", "active_pipe_burst"):
        R = [r for r in rows if r["test"] == test and r.get("req") and r.get("resp")]
        if not R:
            continue
        print(f"\n{test}: {len(R)} orders, accepted {sum(1 for r in R if r['post']['ok'])}, errors",
              collections.Counter((r["post"]["error"] or "")[:60] for r in R if not r["post"]["ok"]).most_common(3))
        P = [r for r in R if r.get("creq") and r["creq"] < r["resp"]["t"]]
        if P:
            print("  cancel reached the wire before the post's response:", len(P), "of", len(R))
        print("  POST  send -> response (httpx)         ", q([r["resp"]["t"] - r["req"] for r in R]))
        print("  POST  send -> on the book (exchange ts)", q([r["on_book"][0] - r["req"] for r in R if r.get("on_book")]))
        print("  POST  on the book -> response          ", q([r["resp"]["t"] - r["on_book"][0] for r in R if r.get("on_book")]))
        C = [r for r in R if r.get("creq") and r.get("cresp")]
        print("  CANCEL send -> response                ", q([r["cresp"]["t"] - r["creq"] for r in C]))
        print("  CANCEL send -> off the book (exch ts)  ", q([r["off_book"][0] - r["creq"] for r in C if r.get("off_book")]))
        print("  CANCEL off the book -> response        ", q([r["cresp"]["t"] - r["off_book"][0] for r in C if r.get("off_book")]))
        W = [r for r in R if r.get("p_wire_in")]
        if W:
            print("  wire: POST first byte out -> first byte in   ", q([r["p_wire_in"] - r["p_wire_out"] for r in W]))
            WC = [r for r in W if r.get("c_wire_in")]
            if WC:
                print("  wire: CANCEL first byte out -> first byte in ", q([r["c_wire_in"] - r["c_wire_out"] for r in WC]))
                print("  wire: CANCEL reply in -> POST reply in        ", q([r["p_wire_in"] - r["c_wire_in"] for r in WC]))
            print("  wire: response on the wire -> httpx hook     ", q([r["resp"]["t"] - r["p_wire_in"] for r in W]))
            print("  wire: on the book -> response on the wire    ", q([r["p_wire_in"] - r["on_book"][0] for r in W if r.get("on_book")]))
        hs = collections.Counter(json.dumps(r["resp"]["h"].get("poly-ratelimit-tier")) for r in R)
        rem = [float(r["resp"]["h"]["poly-ratelimit-remaining"]) for r in R if "poly-ratelimit-remaining" in r["resp"]["h"]]
        print("  rate-limit tier", dict(hs), "| remaining min", min(rem) if rem else None,
              "| warnings", sum(1 for r in R if r["resp"]["h"].get("poly-ratelimit-warning")),
              "| ports", sorted({r["resp"]["port"] for r in R}))
    sample = next((r for r in rows if r.get("resp")), None)
    if sample:
        print("\nresponse headers seen:", sample["resp"]["h"])


if __name__ == "__main__":
    main()

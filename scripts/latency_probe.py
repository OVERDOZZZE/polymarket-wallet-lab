"""Live latency probe: network round trips and feed delays from THIS machine, measured the leanest way (no orders).

    .venv/bin/python -m scripts.latency_probe --site dublin [--minutes 15]       (on the server, from ~/app)

1. Network (sequential, before any feed runs, so nothing competes): per host
     dns       getaddrinfo (first call, then cached calls)
     tcp       TCP connect to the resolved IP (TCP_NODELAY), 20 x: ~1 round trip to the host's EDGE (Cloudflare /
               AWS front ends answer near us; the origin can be farther)
     tls       TLS handshake on a fresh TCP connection, 10 x
     http      a tiny GET on ONE kept-alive connection (http.client, no pooling layer), 40 x after 2 warm-ups:
               our request -> the origin's answer, i.e. the real round trip to the venue's servers
2. Feeds, all at once, ONE PROCESS PER FEED (uvloop on Linux; compression off; the socket loop only stamps
   time.time_ns() after recv() and keeps the raw frame; JSON is parsed after the run), for --minutes:
     age    our receive time - the venue's own timestamp in the message (clock-corrected, see 3)
     gap    time between consecutive messages (a feed's freezes), and messages/s
     ping   websocket ping -> pong every 5 s (queues behind data frames on busy feeds: an upper bound of the RTT)
     connect  TCP + TLS + websocket upgrade of each (re)connection
3. Clock: the offset to true time is sampled every 30 s during the run (chrony on Linux, ~1 us on AWS; elsewhere
   SNTP, shortest of 5 queries) and each message's age is corrected by the offset interpolated at its receive
   time (a Windows clock drifts ~2 ms/min). Check: a feed's minimum age should be ~ its minimum ping / 2.

Output: data/latency/probe-<site>-<utc>.json: every measure as {n, mean, min, p1..p99.9, max, q (101 quantiles)}.
Prints timings only; never reads keys.
"""
import argparse
import asyncio
import http.client
import json
import multiprocessing as mp
import os
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import CLOB_MARKET_WS, COINBASE_WS, GAMMA, KRAKEN_WS, LATENCY_DIR, RTDS_WS  # noqa: E402

OUT_DIR = LATENCY_DIR

# name: (host, port, path or None for a TCP/TLS-only host)
HOSTS = {
    "polymarket_clob": ("clob.polymarket.com", 443, "/time"),
    "polymarket_ws": ("ws-subscriptions-clob.polymarket.com", 443, None),
    "polymarket_rtds": ("ws-live-data.polymarket.com", 443, None),
    "polymarket_gamma": ("gamma-api.polymarket.com", 443, "/events?limit=1"),
    "polymarket_data": ("data-api.polymarket.com", 443, "/trades?limit=1"),
    "binance_spot": ("api.binance.com", 443, "/api/v3/time"),
    "binance_spot_ws": ("stream.binance.com", 9443, None),
    "binance_fut": ("fapi.binance.com", 443, "/fapi/v1/time"),
    "binance_fut_ws": ("fstream.binance.com", 443, None),
    "coinbase": ("api.exchange.coinbase.com", 443, "/time"),
    "coinbase_ws": ("ws-feed.exchange.coinbase.com", 443, None),
    "kraken": ("api.kraken.com", 443, "/0/public/Time"),
    "kraken_ws": ("ws.kraken.com", 443, None),
}

CB_TICKER = {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}
CB_MATCHES = {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches"]}
# name: (urls tried in order until one sends data, subscribe message, text ping or None)
FEEDS = {
    "binance_spot_book": (["wss://stream.binance.com:9443/ws/btcusdt@bookTicker"], None, None),
    "binance_spot_trade": (["wss://stream.binance.com:9443/ws/btcusdt@trade"], None, None),
    "binance_fut_trade": (["wss://fstream.binance.com/market/ws/btcusdt@aggTrade",
                           "wss://fstream.binance.com/ws/btcusdt@aggTrade"], None, None),
    "binance_fut_book": (["wss://fstream.binance.com/public/ws/btcusdt@bookTicker",
                          "wss://fstream.binance.com/market/ws/btcusdt@bookTicker",
                          "wss://fstream.binance.com/ws/btcusdt@bookTicker"], None, None),
    "coinbase": ([COINBASE_WS], CB_TICKER, None),
    "kraken": ([KRAKEN_WS], {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"],
                                                              "event_trigger": "bbo"}}, None),
    "kraken_trade": ([KRAKEN_WS], {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}},
                     None),
    # the recorder's two subscriptions: the symbol filter alone sends a snapshot and then almost nothing
    "chainlink": ([RTDS_WS], {"action": "subscribe", "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
        {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": "btc/usd"})}]}, "PING"),
    "polymarket_book": ([CLOB_MARKET_WS], "btc5m", "PING"),
}


# ------------------------------------------------------------------ stats
def summary(xs, quantiles=True):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}

    def q(p):
        k = (n - 1) * p
        i = int(k)
        j = min(i + 1, n - 1)
        return xs[i] + (xs[j] - xs[i]) * (k - i)
    out = {"n": n, "mean": sum(xs) / n, "min": xs[0]}
    for p in (1, 5, 10, 25, 50, 75, 90, 95, 99, 99.9):
        out[f"p{p:g}"] = q(p / 100)
    out["max"] = xs[-1]
    if quantiles:
        out["q"] = [round(q(i / 100), 3) for i in range(101)]
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out.items()}


# ------------------------------------------------------------------ clock
def clock_info():
    """(offset_ms to ADD to local time, description). chrony on Linux, else SNTP."""
    if sys.platform != "win32":
        try:
            t = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
            line = next(x for x in t.splitlines() if x.startswith("System time"))
            sec = float(line.split(":")[1].split()[0])
            slow = "slow" in line                          # local clock is behind true time: add
            return (sec * 1000 if slow else -sec * 1000), "chrony: " + line.split(":", 1)[1].strip()
        except Exception:
            pass
    from backend.util import ntp_offset_ms
    o = ntp_offset_ms(samples=5)
    return (o or 0.0), f"sntp offset {o} ms"


# ------------------------------------------------------------------ network
def net_host(host, port, path):
    r = {"host": host, "port": port}
    t0 = time.perf_counter()
    ai = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    r["dns_first_ms"] = (time.perf_counter() - t0) * 1000
    dns = []
    for _ in range(5):
        t0 = time.perf_counter()
        socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        dns.append((time.perf_counter() - t0) * 1000)
    r["dns_cached"] = summary(dns, False)
    ip = ai[0][4][0]
    r["ip"] = ip
    tcp, tls = [], []
    ctx = ssl.create_default_context()
    for i in range(30):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(5)
        try:
            t0 = time.perf_counter()
            s.connect((ip, port))
            t1 = time.perf_counter()
            tcp.append((t1 - t0) * 1000)
            if i < 10:
                t0 = time.perf_counter()
                ss = ctx.wrap_socket(s, server_hostname=host)
                tls.append((time.perf_counter() - t0) * 1000)
                ss.close()
        except Exception as e:
            r.setdefault("errors", []).append(f"{type(e).__name__}: {e}"[:120])
        finally:
            s.close()
        time.sleep(0.05)
    r["tcp_connect"] = summary(tcp, False)
    r["tls_handshake"] = summary(tls, False)
    if path:
        hs = []
        try:
            c = http.client.HTTPSConnection(host, port, timeout=10, context=ctx)
            c.connect()
            c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hdr = {"User-Agent": "Mozilla/5.0 (pmtool latency probe)", "Accept": "application/json",
                   "Connection": "keep-alive"}
            for i in range(42):
                t0 = time.perf_counter()
                c.request("GET", path, headers=hdr)
                resp = c.getresponse()
                resp.read()
                dt = (time.perf_counter() - t0) * 1000
                if i >= 2:                                 # first two: connection warm-up
                    hs.append(dt)
                r["http_status"] = resp.status
                if resp.getheader("Connection", "").lower() == "close":
                    c.close()
                    c = http.client.HTTPSConnection(host, port, timeout=10, context=ctx)
                time.sleep(0.1)
            c.close()
        except Exception as e:
            r.setdefault("errors", []).append(f"http {type(e).__name__}: {e}"[:120])
        r["http_keepalive"] = summary(hs, False)
    return r


# ------------------------------------------------------------------ feeds
def _btc5m_tokens(start):
    import requests
    slug = f"btc-updown-5m-{start}"
    js = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10).json()
    return slug, json.loads(js[0]["clobTokenIds"]) if js else []


def feed_proc(name, seconds, q):
    """One feed in its own process: stamp + keep raw frames; parse after the run."""
    if sys.platform != "win32":
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass
    import websockets
    urls, sub, ping_txt = FEEDS[name]
    frames, pings, connects, errors = [], [], [], []      # frames: (recv ns, raw, connection number)
    used = {"url": None}
    end = time.time() + seconds

    async def one(url, subscribe, until):
        t0 = time.perf_counter()
        async with websockets.connect(url, ping_interval=None, max_size=None, max_queue=None, open_timeout=10,
                                      compression=None) as ws:
            connects.append((time.perf_counter() - t0) * 1000)
            if subscribe:
                await ws.send(json.dumps(subscribe))

            async def pinger():
                while True:
                    await asyncio.sleep(5)
                    t = time.perf_counter()
                    w = await ws.ping()
                    await asyncio.wait_for(w, 10)
                    pings.append((time.perf_counter() - t) * 1000)
                    if ping_txt:
                        await ws.send(ping_txt)
            pt = asyncio.create_task(pinger())
            got = 0
            try:
                while time.time() < until:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, until - time.time()))
                    except asyncio.TimeoutError:
                        if not got:
                            return False
                        continue
                    t = time.time_ns()
                    frames.append((t, raw, len(connects)))
                    got += 1
                    if (name == "coinbase" and isinstance(raw, str) and "subscription limit reached" in raw):
                        await ws.send(json.dumps(CB_MATCHES))
            finally:
                pt.cancel()
            return got > 0

    async def run():
        if name == "polymarket_book":                   # current window's tokens, new connection each window
            while time.time() < end:
                start = int(time.time()) // 300 * 300
                try:
                    slug, toks = await asyncio.to_thread(_btc5m_tokens, start)
                    used["url"] = CLOB_MARKET_WS
                    await one(CLOB_MARKET_WS, {"assets_ids": toks, "type": "market"}, min(end, start + 300))
                except Exception as e:
                    errors.append(f"{type(e).__name__}: {e}"[:160])
                    await asyncio.sleep(1)
            return
        for url in urls:                                # first url that sends data within 8 s
            try:
                ok = await one(url, sub, time.time() + 8)
            except Exception as e:
                errors.append(f"{url}: {type(e).__name__}: {e}"[:160])
                ok = False
            if ok:
                used["url"] = url
                break
        while used["url"] and time.time() < end:
            try:
                await one(used["url"], sub, end)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}"[:160])
                await asyncio.sleep(0.5)

    asyncio.run(run())
    q.put((name, used["url"], frames, pings, connects, errors))


def _iso_ms(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def offset_at(samples):
    """samples [(local ms, offset ms)] -> f(local ms) = offset, linear in between, flat outside."""
    samples = sorted(samples)

    def f(t):
        if t <= samples[0][0]:
            return samples[0][1]
        for (a, oa), (b, ob) in zip(samples, samples[1:]):
            if t <= b:
                return oa + (ob - oa) * (t - a) / (b - a) if b > a else ob
        return samples[-1][1]
    return f


def ages(name, frames, offset):
    """[(recv_ms corrected, {label: age ms})] per data message; labels per feed. offset: f(local ms) -> ms."""
    out = []
    for t_ns, raw, _ in frames:
        r = t_ns / 1e6
        r += offset(r)
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        a = {}
        if name in ("binance_spot_trade", "binance_fut_trade", "binance_fut_book"):
            if isinstance(d, dict) and "E" in d:
                a["event"] = r - d["E"]                   # Binance's publish (event) time -> us
                if "T" in d:
                    a["trade"] = r - d["T"]               # the trade / book update itself -> us
                    a["venue_internal"] = d["E"] - d["T"]
        elif name == "binance_spot_book":
            a["none"] = None                            # no timestamp in this stream: gaps and rate only
        elif name == "coinbase":
            if isinstance(d, dict) and d.get("time") and d.get("type") in ("ticker", "match", "last_match"):
                a["event"] = r - _iso_ms(d["time"])
        elif name == "kraken":                          # ticker's timestamp: the update (bbo trigger)
            if isinstance(d, dict) and d.get("channel") == "ticker" and d.get("type") == "update":
                ts = (d.get("data") or [{}])[0].get("timestamp")
                if ts:
                    a["event"] = r - _iso_ms(ts)
        elif name == "kraken_trade":
            if isinstance(d, dict) and d.get("channel") == "trade" and d.get("type") == "update":
                ts = (d.get("data") or [{}])[-1].get("timestamp")
                if ts:
                    a["trade"] = r - _iso_ms(ts)
        elif name == "chainlink":
            if (isinstance(d, dict) and d.get("topic") == "crypto_prices_chainlink"
                    and (d.get("payload") or {}).get("symbol") == "btc/usd"):
                if d.get("timestamp"):
                    a["event"] = r - d["timestamp"]       # RTDS publish -> us
                p = d.get("payload") or {}
                if p.get("timestamp"):
                    a["observation"] = r - p["timestamp"]  # Chainlink's observation time (whole seconds) -> us
        elif name == "polymarket_book":
            for e in d if isinstance(d, list) else [d]:
                if isinstance(e, dict) and e.get("timestamp"):
                    a.setdefault("event", r - float(e["timestamp"]))
                    a.setdefault(e.get("event_type", "?"), r - float(e["timestamp"]))
        if a:
            out.append((r, a))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, help="label of this machine, e.g. dublin or home")
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--skip-net", action="store_true")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    res = {"site": a.site, "utc": stamp, "platform": sys.platform, "cpus": os.cpu_count(), "minutes": a.minutes}
    off0, desc0 = clock_info()
    clock = [(time.time() * 1000, off0)]
    res["clock_start"] = desc0
    res["load_start"] = os.getloadavg() if hasattr(os, "getloadavg") else None
    print(f"clock: {desc0}", flush=True)

    if not a.skip_net:
        res["network"] = {}
        for k, (h, p, path) in HOSTS.items():
            try:
                res["network"][k] = r = net_host(h, p, path)
                print(f"{k:18s} tcp p50 {r['tcp_connect'].get('p50')}  tls p50 {r['tls_handshake'].get('p50')}  "
                      f"http p50 {r.get('http_keepalive', {}).get('p50')}  {r.get('errors', '')}", flush=True)
            except Exception as e:
                res["network"][k] = {"error": f"{type(e).__name__}: {e}"}
                print(k, "error", e, flush=True)

    secs = a.minutes * 60
    q = mp.Queue()
    procs = [mp.Process(target=feed_proc, args=(n, secs, q), daemon=True) for n in FEEDS]
    print(f"{time.strftime('%H:%M:%S')} feeds for {a.minutes:g} min ...", flush=True)
    for p in procs:
        p.start()
    got = {}
    deadline = time.time() + secs + 120
    next_clock = time.time() + 30
    while len(got) < len(procs) and time.time() < deadline:
        if time.time() >= next_clock and len(got) == 0:
            o, _ = clock_info()
            clock.append((time.time() * 1000, o))
            next_clock = time.time() + 30
        try:
            item = q.get(timeout=2)
            got[item[0]] = item[1:]
        except Exception:
            pass
    for p in procs:
        p.join(timeout=5)
    off1, desc1 = clock_info()
    clock.append((time.time() * 1000, off1))
    res["clock_end"] = desc1
    res["load_end"] = os.getloadavg() if hasattr(os, "getloadavg") else None
    offset = offset_at(clock)
    offs = [o for _, o in clock]
    res["clock_samples"] = len(clock)
    res["clock_offset_ms_range"] = [round(min(offs), 3), round(max(offs), 3)]

    res["feeds"] = {}
    for name in FEEDS:
        if name not in got:
            res["feeds"][name] = {"error": "no result"}
            continue
        url, frames, pings, connects, errors = got[name]
        rows = ages(name, frames, offset)
        labels = sorted({k for _, a_ in rows for k in a_ if a_[k] is not None})
        if name == "chainlink":                       # every symbol arrives on this socket: keep BTC's for gaps
            frames = [f for f in frames if isinstance(f[1], str) and '"btc/usd"' in f[1] and '"update"' in f[1]]
        recv = [t_ns / 1e6 for t_ns, _, _ in frames]
        gaps = [b[0] / 1e6 - x[0] / 1e6 for x, b in zip(frames, frames[1:]) if x[2] == b[2]]   # within a connection
        span = (recv[-1] - recv[0]) / 1000 if len(recv) > 1 else 0
        f = {"url": url, "messages": len(frames), "msgs_per_s": round(len(frames) / span, 2) if span else None,
             "gap": summary(gaps), "ws_ping": summary(pings, False), "connect": summary(connects, False),
             "errors": errors[:10], "age": {k: summary([a_[k] for _, a_ in rows if a_.get(k) is not None])
                                           for k in labels}}
        ev_ = f["age"].get("trade") or f["age"].get("event") or {}
        if ev_.get("n") and f["ws_ping"].get("n"):      # one-way floor: should be ~0 (clock + stamping check)
            f["floor_check_ms"] = round(ev_["min"] - f["ws_ping"]["min"] / 2, 2)
        res["feeds"][name] = f
        ev = f["age"].get("event") or f["age"].get("trade") or {}
        print(f"{name:20s} {len(frames):8d} msgs  age p50 {ev.get('p50')} p99 {ev.get('p99')}  "
              f"gap p99 {f['gap'].get('p99')} max {f['gap'].get('max')}  ping p50 {f['ws_ping'].get('p50')}  "
              f"{errors[:1]}", flush=True)
    path = OUT_DIR / f"probe-{a.site}-{stamp}.json"
    path.write_text(json.dumps(res, indent=1))
    print(f"wrote {path}", flush=True)


if __name__ == "__main__":
    main()

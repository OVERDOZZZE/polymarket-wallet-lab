#!/usr/bin/env python3
"""
feed_latency_test.py  -  which BTC price feed shows a move FIRST at *your* machine?

Run this on the same VPS your bot runs on (Dublin), because the network path is part of the answer.

What it measures (all timestamps are taken on ONE local clock, so no clock-sync needed for the
lead/lag results):

  1. Network: TCP connect time to each feed's host (~1 round trip to the edge).
  2. Feed stats: tick rate and gaps (a feed that only updates 1x/sec is slow between updates).
  3. Lead/lag by cross-correlation of price changes  -> "B shows moves X ms after A".
  4. Spike race: detect spikes (>= N bps within a window) in every feed independently, group the
     ones that belong to the same real spike, and measure who fired first and by how many ms.
     This is the number your bot actually cares about.
  5. Age of ticks (local receive time minus the exchange's own timestamp), corrected with an SNTP
     clock-offset estimate. Indicative only, depends on clock accuracy.

Because net speed = network latency + venue reaction + feed publishing delay, (3) and (4)
already include all of it. (1) and (5) just help explain *why*.

Sources:
  binance       Binance spot BTCUSDT trade stream
  binance_book  Binance spot BTCUSDT bookTicker (mid of best bid/ask)
  coinbase      Coinbase Exchange BTC-USD ticker
  chainlink     Chainlink BTC/USD via Polymarket RTDS (public, no key; Polymarket marks it deprecated)
  chainlink_pb  Same Chainlink price via Polymarket's newer PolyBolt socket (needs your CLOB API
                creds in env: POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE)

Install:   pip install websockets numpy
Run:       python feed_latency_test.py --duration 240
Self-test: python feed_latency_test.py --selftest
"""
import argparse
import asyncio
import calendar
import json
import os
import socket
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    import numpy as np
except ImportError:
    sys.exit("Missing dependency. Run: pip install websockets numpy")

try:
    import orjson
    _loads = orjson.loads
except ImportError:
    _loads = json.loads

ORDER = ["binance_book", "binance", "coinbase", "chainlink", "chainlink_pb"]
TICKS = defaultdict(list)   # src -> [(t_ms_since_start, price)]
AGES = defaultdict(list)    # label -> [(wall_ms_at_receipt, exchange_ts_ms)]
SAMPLE_MSG = {}             # src -> first raw message (for debugging format changes)
SKIPPED = defaultdict(int)
T0_NS = time.perf_counter_ns()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def rec(src, price, t_ns, wall_ns, exch_ms=None):
    TICKS[src].append(((t_ns - T0_NS) / 1e6, price))
    if exch_ms:
        AGES[src].append((wall_ns / 1e6, float(exch_ms)))


def iso_to_ms(s):
    try:
        s = s.rstrip("Z")
        if "." in s:
            base, frac = s.split(".")
            frac = (frac + "000000")[:6]
        else:
            base, frac = s, "000000"
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(dt.timetuple()) * 1000 + int(frac) / 1000.0
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------------------
async def binance_collector(connect, kw, host, want_trade, want_book):
    streams = []
    if want_trade:
        streams.append("btcusdt@trade")
    if want_book:
        streams.append("btcusdt@bookTicker")
    url = f"wss://{host}/stream?streams={'/'.join(streams)}"
    while True:
        try:
            async with connect(url, **kw) as ws:
                log("binance connected")
                async for raw in ws:
                    t = time.perf_counter_ns()
                    w = time.time_ns()
                    m = _loads(raw)
                    d = m.get("data", m)
                    if d.get("e") == "trade":
                        SAMPLE_MSG.setdefault("binance", str(raw)[:200])
                        rec("binance", float(d["p"]), t, w, d.get("T"))
                    elif "b" in d and "a" in d:
                        SAMPLE_MSG.setdefault("binance_book", str(raw)[:200])
                        rec("binance_book", (float(d["b"]) + float(d["a"])) / 2.0, t, w)
                    else:
                        SKIPPED["binance"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"binance error: {e!r} (retrying)")
            await asyncio.sleep(1)


async def coinbase_collector(connect, kw):
    url = "wss://ws-feed.exchange.coinbase.com"
    # Coinbase caps 'ticker' subscriptions per IP; if the bots on this box already use them up,
    # fall back to 'matches' (every trade, real-time) which has its own limit.
    channel = "ticker"
    while True:
        sub = json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": [channel]})
        try:
            async with connect(url, **kw) as ws:
                await ws.send(sub)
                log(f"coinbase connected ({channel})")
                async for raw in ws:
                    t = time.perf_counter_ns()
                    w = time.time_ns()
                    m = _loads(raw)
                    if m.get("type") in ("ticker", "match", "last_match") and "price" in m:
                        SAMPLE_MSG.setdefault("coinbase", str(raw)[:200])
                        rec("coinbase", float(m["price"]), t, w, iso_to_ms(m.get("time", "")))
                    else:
                        SKIPPED["coinbase"] += 1
                        if m.get("type") == "error":
                            log("coinbase error msg:", str(raw)[:200])
                            if "subscription limit" in str(m.get("reason", "")) and channel == "ticker":
                                channel = "matches"
                                log("coinbase: ticker limit reached on this IP, switching to 'matches'")
                                break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"coinbase error: {e!r} (retrying)")
            await asyncio.sleep(1)


async def chainlink_rtds_collector(connect, kw):
    url = "wss://ws-live-data.polymarket.com"
    # A symbol-filtered subscription only replays a ~60 s backlog; live updates come from the
    # unfiltered one (all symbols, ~1/s each), so subscribe to both and filter btc/usd below.
    sub = json.dumps({"action": "subscribe", "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
        {"topic": "crypto_prices_chainlink", "type": "*",
         "filters": json.dumps({"symbol": "btc/usd"})}]})
    while True:
        pinger = None
        try:
            async with connect(url, **kw) as ws:
                await ws.send(sub)
                log("chainlink (RTDS) connected")

                async def ping():
                    while True:
                        await asyncio.sleep(5)
                        await ws.send("PING")
                pinger = asyncio.create_task(ping())
                async for raw in ws:
                    t = time.perf_counter_ns()
                    w = time.time_ns()
                    if not raw or raw in ("PONG", b"PONG"):
                        continue
                    try:
                        m = _loads(raw)
                    except Exception:
                        SKIPPED["chainlink"] += 1
                        continue
                    if m.get("topic") != "crypto_prices_chainlink":
                        SKIPPED["chainlink"] += 1
                        continue
                    pl = m.get("payload") or {}
                    if str(pl.get("symbol", "btc/usd")).lower() != "btc/usd":
                        SKIPPED["chainlink"] += 1
                        continue
                    val = pl.get("value", pl.get("price"))
                    if val is None or isinstance(val, (list, dict)):
                        SKIPPED["chainlink"] += 1       # snapshot / backfill frames
                        continue
                    ts = pl.get("timestamp")
                    if ts and (w / 1e6 - float(ts)) > 15000:
                        SKIPPED["chainlink"] += 1       # old backfilled tick
                        continue
                    SAMPLE_MSG.setdefault("chainlink", str(raw)[:200])
                    rec("chainlink", float(val), t, w, ts)
                    if m.get("timestamp"):
                        AGES["chainlink_relay"].append((w / 1e6, float(m["timestamp"])))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"chainlink RTDS error: {e!r} (retrying)")
            await asyncio.sleep(2)
        finally:
            if pinger:
                pinger.cancel()


async def chainlink_polybolt_collector(connect, kw):
    key = os.environ.get("POLY_API_KEY")
    sec = os.environ.get("POLY_API_SECRET")
    pas = os.environ.get("POLY_API_PASSPHRASE")
    if not (key and sec and pas):
        log("chainlink_pb skipped: set POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE")
        return
    url = "wss://ws-live-v2.polymarket.com/ws"
    auth = json.dumps({"op": "auth", "auth": {"apiKey": key, "secret": sec, "passphrase": pas}})
    sub = json.dumps({"op": "subscribe", "subscriptions": [
        {"channel": "price.crypto", "filter": {"symbol": "btcusd"}}]})
    while True:
        try:
            async with connect(url, **kw) as ws:
                await ws.send(auth)
                await ws.send(sub)
                log("chainlink_pb (PolyBolt) connected")
                async for raw in ws:
                    t = time.perf_counter_ns()
                    w = time.time_ns()
                    try:
                        m = _loads(raw)
                    except Exception:
                        continue
                    if m.get("channel") != "price.crypto" or m.get("snapshot"):
                        SKIPPED["chainlink_pb"] += 1
                        if m.get("op") == "error" or "error" in m:
                            log("PolyBolt msg:", str(raw)[:200])
                        continue
                    pl = m.get("payload") or {}
                    if str(pl.get("symbol", "btc/usd")).lower() != "btc/usd":
                        SKIPPED["chainlink"] += 1
                        continue
                    val = pl.get("full_accuracy_value") or pl.get("value")
                    if val is None or isinstance(val, (list, dict)):
                        SKIPPED["chainlink_pb"] += 1
                        continue
                    ts = pl.get("timestamp")
                    if isinstance(ts, str):
                        ts = iso_to_ms(ts)
                    SAMPLE_MSG.setdefault("chainlink_pb", str(raw)[:200])
                    rec("chainlink_pb", float(val), t, w, ts)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"chainlink_pb error: {e!r} (retrying)")
            await asyncio.sleep(2)


# --------------------------------------------------------------------------------------
# Pre-flight probes
# --------------------------------------------------------------------------------------
def sntp_offset(server="time.cloudflare.com", n=5):
    """Return (offset_ms, delay_ms): server_time - local_time. Best-of-n by lowest delay."""
    NTP_EPOCH = 2208988800
    best = None
    for _ in range(n):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            t1 = time.time()
            s.sendto(b"\x1b" + 47 * b"\0", (server, 123))
            data, _ = s.recvfrom(48)
            t4 = time.time()
            s.close()
            u = struct.unpack("!12I", data)
            t2 = u[8] - NTP_EPOCH + u[9] / 2**32
            t3 = u[10] - NTP_EPOCH + u[11] / 2**32
            offset = ((t2 - t1) + (t3 - t4)) / 2
            delay = (t4 - t1) - (t3 - t2)
            if best is None or delay < best[1]:
                best = (offset * 1000, delay * 1000)
        except Exception:
            pass
    return best


def tcp_rtt(host, port, n=10):
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except Exception as e:
        return host, None, []
    samples = []
    for _ in range(n):
        try:
            t = time.perf_counter()
            s = socket.create_connection((ip, port), timeout=3)
            samples.append((time.perf_counter() - t) * 1000)
            s.close()
        except Exception:
            pass
        time.sleep(0.05)
    return host, ip, samples


# --------------------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------------------
def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def xcorr_lag(a, b, g, maxlag_ms):
    """Lag (ms) at which corr(a(t), b(t+lag)) peaks. Positive => b reacts LATER than a."""
    ml = int(maxlag_ms / g)
    lags = np.arange(-ml, ml + 1)
    cs = np.zeros(len(lags))
    n = len(a)
    for i, L in enumerate(lags):
        if L >= 0:
            x, y = a[:n - L], b[L:]
        else:
            x, y = a[-L:], b[:n + L]
        sx, sy = x.std(), y.std()
        if sx > 0 and sy > 0:
            cs[i] = np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy)
    i = int(np.argmax(cs))
    delta = 0.0
    if 0 < i < len(cs) - 1:
        den = cs[i - 1] - 2 * cs[i] + cs[i + 1]
        if den != 0:
            delta = 0.5 * (cs[i - 1] - cs[i + 1]) / den
    return (lags[i] + delta) * g, float(cs[i])


def detect_events(t, p, window_ms, thr_bps, refractory_ms):
    events = []
    j = 0
    last_t = -1e18
    for i in range(len(t)):
        lim = t[i] - window_ms
        while j + 1 <= i and t[j + 1] <= lim:
            j += 1
        if t[j] > lim:
            continue
        chg = (p[i] - p[j]) / p[i] * 1e4
        if abs(chg) >= thr_bps and (t[i] - last_t) >= refractory_ms:
            events.append((float(t[i]), 1 if chg > 0 else -1))
            last_t = t[i]
    return events


def cluster_events(ev_by_src, match_ms):
    allev = sorted((t, s, sg) for s, evs in ev_by_src.items() for (t, sg) in evs)
    used = [False] * len(allev)
    clusters = []
    for i, (t, s, sg) in enumerate(allev):
        if used[i]:
            continue
        used[i] = True
        cl = {s: t}
        for k in range(i + 1, len(allev)):
            t2, s2, sg2 = allev[k]
            if t2 - t > match_ms:
                break
            if not used[k] and sg2 == sg and s2 not in cl:
                cl[s2] = t2
                used[k] = True
        clusters.append(cl)
    return clusters


def analyze(args, ticks, ages, offset_ms):
    out = []
    P = out.append
    series = {}
    for s, v in ticks.items():
        if len(v) >= 20:
            series[s] = (np.array([x[0] for x in v]), np.array([x[1] for x in v]))
    srcs = [s for s in ORDER if s in series]
    P("=" * 78)
    P("FEED STATS")
    P("=" * 78)
    P(f"{'source':<14}{'ticks':>8}{'ticks/s':>9}{'gap med':>9}{'gap p95':>9}"
      f"{'age p10':>9}{'age med':>9}{'age p90':>9}   (ms)")
    for s in srcs:
        t, _ = series[s]
        gaps = np.diff(t)
        rate = len(t) / max((t[-1] - t[0]) / 1000, 1e-9)
        a = np.array([w + offset_ms - e for (w, e) in ages.get(s, [])])
        a = a[(a > -2000) & (a < 120000)] if len(a) else a
        age_txt = (f"{pct(a,10):9.0f}{pct(a,50):9.0f}{pct(a,90):9.0f}" if len(a) > 5
                   else f"{'-':>9}{'-':>9}{'-':>9}")
        P(f"{s:<14}{len(t):8d}{rate:9.1f}{pct(gaps,50):9.1f}{pct(gaps,95):9.1f}{age_txt}")
    if ages.get("chainlink_relay"):
        a = np.array([w + offset_ms - e for (w, e) in ages["chainlink_relay"]])
        P(f"chainlink relay age (Polymarket send-time -> here): med {pct(a,50):.0f} ms, "
          f"p10 {pct(a,10):.0f}, p90 {pct(a,90):.0f}")
    P("age = local receive time (SNTP-corrected) minus the venue's own timestamp. Treat as ")
    P("indicative: it is only as accurate as the clock offset, and venues stamp at different stages.")
    P("")

    if len(srcs) < 2:
        P("Need at least 2 live sources for lead/lag analysis.")
        return out

    t_start = max(series[s][0][0] for s in srcs) + 1000
    t_end = min(series[s][0][-1] for s in srcs)
    if t_end - t_start < 20000:
        P("Not enough overlapping data for lead/lag analysis (need >20 s). Run longer.")
        return out

    # ------------------------------------------------------------------ cross-correlation
    g = args.xcorr_grid_ms
    tg = np.arange(t_start, t_end, g)
    gridp = {}
    gapmed = {}
    for s in srcs:
        t, p = series[s]
        gridp[s] = p[np.searchsorted(t, tg, side="right") - 1]
        gapmed[s] = float(np.median(np.diff(t)))

    def incr(s, win_ms):
        m = max(1, int(win_ms / g))
        Pg = gridp[s]
        d = np.zeros_like(Pg)
        d[m:] = (Pg[m:] - Pg[:-m]) / Pg[m:] * 1e4
        return d

    P("=" * 78)
    P(f"LEAD/LAG BY CROSS-CORRELATION  (grid {g:g} ms; price change over >= {args.xcorr_inc_ms:g} ms,")
    P("widened automatically for slow feeds such as Chainlink)")
    P("=" * 78)
    P("Positive = second feed shows moves LATER than the first. Fast feeds: roughly +-10-20 ms.")
    P("Slow feeds (1 update/s): treat as rough; the spike race below is the better number.")
    n = len(tg)
    h = n // 2
    for i in range(len(srcs)):
        for j in range(i + 1, len(srcs)):
            a, b = srcs[i], srcs[j]
            win = max(args.xcorr_inc_ms, min(1000.0, max(gapmed[a], gapmed[b])))
            xa, xb = incr(a, win), incr(b, win)
            L, r = xcorr_lag(xa, xb, g, args.xcorr_maxlag_ms)
            L1, _ = xcorr_lag(xa[:h], xb[:h], g, args.xcorr_maxlag_ms)
            L2, _ = xcorr_lag(xa[h:], xb[h:], g, args.xcorr_maxlag_ms)
            who = "LATER than" if L > 0 else "EARLIER than"
            P(f"{b:>13} is {abs(L):6.0f} ms {who:<12} {a:<13} (r={r:.2f}, win {win:.0f} ms; "
              f"1st half {L1:+.0f}, 2nd half {L2:+.0f})")
    P("")

    # ------------------------------------------------------------------ spike race
    for thr in args.spike_bps:
        P("=" * 78)
        P(f"SPIKE RACE  (>= {thr:g} bps within {args.spike_window_ms:g} ms; same-spike match window "
          f"{args.match_window_ms:g} ms)")
        P("=" * 78)
        refr = max(1500.0, args.spike_window_ms)
        evs = {s: detect_events(series[s][0], series[s][1], args.spike_window_ms, thr, refr)
               for s in srcs}
        evs = {s: [(t, sg) for (t, sg) in e if t_start <= t <= t_end] for s, e in evs.items()}
        P("events per feed: " + ", ".join(f"{s}={len(e)}" for s, e in evs.items()))
        cl = cluster_events(evs, args.match_window_ms)
        multi = [c for c in cl if len(c) >= 2]
        P(f"spikes seen by >=1 feed: {len(cl)}, by >=2 feeds: {len(multi)}")
        if len(multi) < 15:
            P("!! fewer than 15 matched spikes: results are noisy. Run longer or lower --spike-bps.")
        if not multi:
            P("")
            continue
        P("")
        P(f"{'source':<14}{'seen':>7}{'first':>8}{'lag med':>9}{'lag p25':>9}{'lag p75':>9}   "
          "(ms behind the first feed to fire)")
        rank = []
        for s in srcs:
            seen = [c for c in multi if s in c]
            lags = np.array([c[s] - min(c.values()) for c in seen])
            firsts = sum(1 for c in seen if c[s] == min(c.values()))
            if len(seen):
                P(f"{s:<14}{len(seen):7d}{100*firsts/len(seen):7.0f}%{pct(lags,50):9.0f}"
                  f"{pct(lags,25):9.0f}{pct(lags,75):9.0f}")
                rank.append((pct(lags, 50), s))
            else:
                P(f"{s:<14}{0:7d}")
        missed = {s: sum(1 for c in multi if s not in c) for s in srcs}
        P("missed spikes (others fired, this feed never did within the window): " +
          ", ".join(f"{s}={v}" for s, v in missed.items()))
        P("")
        P("head-to-head (only spikes both feeds saw):")
        for i in range(len(srcs)):
            for j in range(i + 1, len(srcs)):
                a, b = srcs[i], srcs[j]
                d = np.array([c[b] - c[a] for c in multi if a in c and b in c])
                if len(d) < 5:
                    continue
                lead = a if np.median(d) > 0 else b
                P(f"  {a:>13} vs {b:<13} n={len(d):3d}  {lead} fires first by median "
                  f"{abs(np.median(d)):.0f} ms  (IQR {pct(d,25):+.0f}..{pct(d,75):+.0f}; "
                  f"{a} first in {100*np.mean(d<0):.0f}%)")
        if rank:
            rank.sort()
            P("ranking by median lag: " + " < ".join(f"{s} ({v:.0f} ms)" for v, s in rank))
        P("")
    return out


# --------------------------------------------------------------------------------------
# Self-test with synthetic feeds of KNOWN delays
# --------------------------------------------------------------------------------------
def selftest(args):
    rng = np.random.default_rng(7)
    dur_ms = 300_000
    n = dur_ms
    true = np.zeros(n)
    steps = rng.normal(0, 0.15, n)
    k = 0
    while k < n - 200:
        k += int(rng.exponential(2500)) + 300
        if k >= n - 200:
            break
        amp = rng.choice([-1, 1]) * rng.uniform(12, 35)
        steps[k:k + 80] += amp / 80.0            # a spike ramps in over 80 ms
    true = 100000 + np.cumsum(steps)
    spec = {  # src: (one-way delay ms, sample interval ms, jitter ms)
        "binance_book": (110, 5, 4),
        "binance": (118, 15, 5),
        "coinbase": (60, 60, 8),
        "chainlink": (650, 1000, 30),
    }
    ticks = defaultdict(list)
    for s, (delay, every, jit) in spec.items():
        for ts in range(0, n, every):
            arrive = ts + delay + abs(rng.normal(0, jit))
            ticks[s].append((arrive, float(true[ts])))
        ticks[s].sort()
    print("Synthetic truth (ms after the real move):",
          {s: v[0] for s, v in spec.items()})
    return ticks


# --------------------------------------------------------------------------------------
# Live run
# --------------------------------------------------------------------------------------
async def run_live(args):
    try:
        import websockets
    except ImportError:
        sys.exit("Missing dependency. Run: pip install websockets numpy")
    connect = websockets.connect
    kw = dict(max_size=None, compression=None, ping_interval=20, ping_timeout=20, open_timeout=10)
    want = set(args.sources)
    tasks = []
    if want & {"binance", "binance_book"}:
        tasks.append(asyncio.create_task(binance_collector(
            connect, kw, args.binance_host, "binance" in want, "binance_book" in want)))
    if "coinbase" in want:
        tasks.append(asyncio.create_task(coinbase_collector(connect, kw)))
    if "chainlink" in want:
        tasks.append(asyncio.create_task(chainlink_rtds_collector(connect, kw)))
    if "chainlink_pb" in want:
        tasks.append(asyncio.create_task(chainlink_polybolt_collector(connect, kw)))
    log(f"collecting for {args.duration:.0f} s ... (don't run heavy jobs on this box meanwhile)")
    t_end = time.time() + args.duration
    while time.time() < t_end:
        await asyncio.sleep(min(10, max(0.1, t_end - time.time())))
        log("ticks so far: " + ", ".join(f"{s}={len(TICKS[s])}" for s in ORDER if s in TICKS))
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=240, help="seconds to collect (default 240)")
    ap.add_argument("--sources", default="binance,binance_book,coinbase,chainlink",
                    help="comma list of: binance,binance_book,coinbase,chainlink,chainlink_pb")
    ap.add_argument("--binance-host", default="stream.binance.com:9443")
    ap.add_argument("--spike-bps", default="0.5,1,2", help="comma list of spike thresholds in bps")
    ap.add_argument("--spike-window-ms", type=float, default=1000)
    ap.add_argument("--match-window-ms", type=float, default=2000)
    ap.add_argument("--xcorr-grid-ms", type=float, default=10)
    ap.add_argument("--xcorr-inc-ms", type=float, default=200)
    ap.add_argument("--xcorr-maxlag-ms", type=float, default=2000)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--selftest", action="store_true", help="run analysis on synthetic data")
    args = ap.parse_args()
    args.sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    args.spike_bps = [float(x) for x in args.spike_bps.split(",")]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    lines = []

    if args.selftest:
        ticks = selftest(args)
        lines += analyze(args, ticks, {}, 0.0)
    else:
        log("pre-flight: clock offset + TCP connect times ...")
        so = sntp_offset()
        offset_ms = so[0] if so else 0.0
        lines.append("=" * 78)
        lines.append("SETUP")
        lines.append("=" * 78)
        if so:
            lines.append(f"local clock offset vs time.cloudflare.com: {so[0]:+.1f} ms (SNTP delay {so[1]:.1f} ms)")
        else:
            lines.append("SNTP failed (UDP 123 blocked?) - 'age' columns are uncorrected.")
        probes = []
        if set(args.sources) & {"binance", "binance_book"}:
            h, p = args.binance_host.split(":")
            probes.append((h, int(p)))
        if "coinbase" in args.sources:
            probes.append(("ws-feed.exchange.coinbase.com", 443))
        if "chainlink" in args.sources:
            probes.append(("ws-live-data.polymarket.com", 443))
        if "chainlink_pb" in args.sources:
            probes.append(("ws-live-v2.polymarket.com", 443))
        lines.append(f"{'host':<34}{'ip':<17}{'tcp min':>9}{'tcp med':>9}  (ms; ~1 round trip to the EDGE, not necessarily the origin)")
        for host, port in probes:
            h, ip, smp = tcp_rtt(host, port)
            if smp:
                lines.append(f"{h:<34}{ip:<17}{min(smp):9.1f}{float(np.median(smp)):9.1f}")
            else:
                lines.append(f"{h:<34}{'unreachable':<17}")
        lines.append("")
        print("\n".join(lines), flush=True)
        try:
            asyncio.run(run_live(args))
        except KeyboardInterrupt:
            log("interrupted - analysing what was collected")
        for s, msg in SAMPLE_MSG.items():
            log(f"sample {s}: {msg}")
        for s, c in SKIPPED.items():
            if c:
                log(f"{s}: {c} non-price/ignored messages")
        for s in args.sources:
            if len(TICKS[s]) < 20:
                log(f"WARNING: {s} produced {len(TICKS[s])} ticks - it will be excluded")
        ticks = TICKS
        # persist raw ticks so the run can be re-analysed or shared
        raw_path = os.path.join(args.out_dir, f"feed_ticks_{stamp}.csv")
        with open(raw_path, "w") as f:
            f.write("src,t_ms,price\n")
            for s, v in ticks.items():
                for t, p in v:
                    f.write(f"{s},{t:.3f},{p}\n")
        log(f"raw ticks saved to {raw_path}")
        lines = lines + analyze(args, ticks, AGES, offset_ms)

    lines.append("=" * 78)
    lines.append("HOW TO READ THIS")
    lines.append("=" * 78)
    lines.append("* Spike race + cross-correlation already contain network + venue reaction + feed delay,")
    lines.append("  because every arrival is stamped on your VPS clock. That is the 'net speed'.")
    lines.append("* A feed 50 ms closer but 51 ms slower to react shows up as +1 ms here. Nothing to add up.")
    lines.append("* Chainlink being hundreds of ms behind is expected: it aggregates several venues first.")
    lines.append("  It matters because the 5-min markets RESOLVE on it, not because it is a fast signal.")
    lines.append("* Run for several minutes, ideally during busy hours, and repeat at a different time.")
    report = "\n".join(lines)
    print()
    print(report)
    if not args.selftest:
        rp = os.path.join(args.out_dir, f"feed_report_{stamp}.txt")
        with open(rp, "w") as f:
            f.write(report + "\n")
        log(f"report saved to {rp}")


if __name__ == "__main__":
    main()
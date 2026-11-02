"""Which BTC feed moves first, as the bot receives it? Lead-lag of Coinbase / Kraken / Binance spot / Binance futures
from a recordings folder (receive time, so network distance is included). Places no orders.

    .venv\\Scripts\\python -m scripts.source_leadlag --dir data/recordings_london --from "2026-11-01 08:20" --to "2026-11-02 02:00"

1. Cross-correlation of 200 ms returns on a 10 ms grid: the lag where corr(a(t), b(t + lag)) peaks; > 0 = a leads b.
2. Spike events (the bot's own trigger: some feed moves >= 2 bps over 1 s; 5 s apart), kept when the move is real
   (>= 2 of Coinbase / Binance spot / futures reach 1.5 bps the same way within -1 .. +1.5 s): for each feed, when its
   own 1 s move first reaches 1.5 bps, relative to the event start; who got there first; and when the 5m window's Up
   mid on Polymarket (`pm`, best bid/ask of price_change messages) first moved 1 c the same way (the makers' reaction).
"""
import argparse
import collections
import sys
from pathlib import Path

import numpy as np
import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402

SRC = ("cb", "kr", "bn", "bf")
NAME = {"cb": "coinbase", "kr": "kraken", "bn": "binance", "bf": "binance_fut"}
GRID = 10


def load(start, end, rec_dir):
    pts = {s: [] for s in SRC}
    pm, up_tok = [], {}
    kr_last = None
    for ms, src, raw in replay.iter_recording(start, end, rec_dir):
        if src == "info":
            d = orjson.loads(raw)
            if "-5m-" in d["slug"]:
                up_tok[d["slug"]] = d["tokens"]["up"]
            continue
        if src.startswith("bk:btc-updown-5m-"):
            slug = src[3:]
            w0 = int(slug.rsplit("-", 1)[1]) * 1000
            if not (w0 <= ms < w0 + 300_000) or slug not in up_tok or b"price_changes" not in raw.encode()[:200]:
                continue
            try:
                d = orjson.loads(raw)
                for c in d.get("price_changes") or ():
                    if c.get("asset_id") == up_tok[slug] and c.get("best_bid") and c.get("best_ask"):
                        bb, ba = float(c["best_bid"]), float(c["best_ask"])
                        if ba - bb <= 0.03 and 0.1 <= bb <= 0.9:
                            pm.append((ms, (bb + ba) / 2, w0))
            except (orjson.JSONDecodeError, TypeError, ValueError):
                pass
            continue
        if src not in pts:
            continue
        try:
            d = orjson.loads(raw)
            if src == "cb":
                if d.get("type") not in ("ticker", "match", "last_match"):
                    continue
                px = float(d["price"])
            elif src == "bn":
                px = (float(d["b"]) + float(d["a"])) / 2
            elif src == "bf":
                px = float(d["p"]) if "p" in d else (float(d["b"]) + float(d["a"])) / 2
            else:
                if d.get("channel") != "ticker":
                    continue
                x = next((x for x in d.get("data") or () if x.get("symbol") == "BTC/USD" and x.get("last")), None)
                if x is None or (x["last"], x.get("volume")) == kr_last:
                    continue
                kr_last = (x["last"], x.get("volume"))
                px = float(x["last"])
        except (KeyError, TypeError, ValueError, orjson.JSONDecodeError):
            continue
        pts[src].append((ms, px))
    return {s: np.array(v) for s, v in pts.items() if len(v) > 100}, np.array(pm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    a = ap.parse_args()
    s, e = replay._ts(a.start), replay._ts(a.end)
    data, pm = load(s, e, a.dir)
    srcs = [x for x in SRC if x in data]
    t0 = max(data[x][0, 0] for x in srcs) + 2000
    t1 = min(data[x][-1, 0] for x in srcs)
    grid = np.arange(t0, t1, GRID)
    lp = {}
    for x in srcs:
        i = np.searchsorted(data[x][:, 0], grid, side="right") - 1
        lp[x] = np.log(data[x][i, 1])
    i = np.searchsorted(pm[:, 0], grid, side="right") - 1
    ok = (i >= 0) & (grid - pm[np.maximum(i, 0), 0] < 3000) & (grid - pm[np.maximum(i, 0), 2] < 300_000)
    pmid = np.where(ok, pm[np.maximum(i, 0), 1], np.nan)
    pwin = np.where(ok, pm[np.maximum(i, 0), 2], np.nan)
    hours = (t1 - t0) / 3.6e6
    print(f"{a.dir} {a.start} -> {a.end}: {hours:.1f} h; messages " +
          ", ".join(f"{NAME[x]} {len(data[x]):,}" for x in srcs))

    # 1. cross-correlation of 200 ms returns
    k = 200 // GRID
    r = {x: lp[x][k:] - lp[x][:-k] for x in srcs}
    lags = np.arange(-60, 61)                     # +-600 ms
    print("\nCross-correlation peak (200 ms returns): row leads column by +ms")
    print(f"{'':12s}" + "".join(f"{NAME[y]:>14s}" for y in srcs))
    for x in srcs:
        row = []
        for y in srcs:
            if x == y:
                row.append("-")
                continue
            ax, ay = r[x], r[y]
            best, bl = -2, 0
            for L in lags:
                u, v = (ax[:len(ax) - L], ay[L:]) if L >= 0 else (ax[-L:], ay[:len(ay) + L])
                c = np.corrcoef(u, v)[0, 1]
                if c > best:
                    best, bl = c, L
            row.append(f"{bl * GRID:+d} ({best:.2f})")
        print(f"{NAME[x]:12s}" + "".join(f"{c:>14s}" for c in row))
    rp = pmid[k:] - pmid[:-k]
    rp = np.where(pwin[k:] == pwin[:-k], rp, np.nan)
    good = ~np.isnan(rp)
    print("\nPolymarket 5m Up mid vs each feed: lag where corr(feed(t), pm(t + lag)) peaks (feed leads by +ms)")
    for x in srcs:
        best, bl = -2, 0
        for L in range(0, 151, 2):
            u, v, g = r[x][:len(rp) - L], rp[L:], good[L:]
            c = np.corrcoef(u[g], v[g])[0, 1]
            if c > best:
                best, bl = c, L
        print(f"  {NAME[x]:12s} {bl * GRID:+5d} ms (corr {best:.2f})")

    # 2. spike events
    w = 1000 // GRID
    mv = {x: np.concatenate([np.zeros(w), lp[x][w:] - lp[x][:-w]]) * 1e4 for x in srcs}
    anym = np.max(np.abs(np.vstack([mv[x] for x in srcs])), axis=0)
    idx = np.flatnonzero(anym >= 2.0)
    events, last = [], -10 ** 9
    for i in idx:
        if i - last >= 5000 // GRID:
            events.append(i)
        last = i if i - last >= 5000 // GRID else last
    first = collections.Counter()
    delay = {x: [] for x in srcs}
    pm_delay, kept = [], []
    for i in events:
        trig = max(srcs, key=lambda x: abs(mv[x][i]))
        sgn = np.sign(mv[trig][i])
        lo, hi = max(0, i - 100), min(len(grid), i + 150)        # -1 s .. +1.5 s
        times = {}
        for x in srcs:
            hit = np.flatnonzero(sgn * mv[x][lo:hi] >= 1.5)
            if len(hit):
                times[x] = (lo + hit[0] - i) * GRID
        if sum(x in times for x in ("cb", "bn", "bf")) < 2:
            continue
        kept.append(i)
        first[min(times, key=times.get)] += 1
        for x in srcs:
            delay[x].append(times.get(x))
        p0 = pmid[i]
        if not np.isnan(p0):
            seg = sgn * (pmid[i:min(len(grid), i + 300)] - p0)
            hit = np.flatnonzero((seg >= 0.01 - 1e-9) & (pwin[i:i + len(seg)] == pwin[i]))
            pm_delay.append(hit[0] * GRID if len(hit) else None)
    n = len(kept)
    print(f"\nSpike events (any feed >= 2 bps / 1 s, 5 s apart): {len(events)}; real (>= 2 of cb/bn/bf confirm): "
          f"{n} ({n / hours:.0f} per hour)")
    print(f"{'feed':12s} {'first %':>8s} {'joins %':>8s} {'p25 ms':>7s} {'p50 ms':>7s} {'p75 ms':>7s}   (time its own move reaches 1.5 bps, vs event start)")
    for x in srcs:
        d = np.array([v for v in delay[x] if v is not None])
        q = np.percentile(d, [25, 50, 75]) if len(d) else [np.nan] * 3
        print(f"{NAME[x]:12s} {100 * first[x] / n:8.1f} {100 * len(d) / n:8.1f} {q[0]:7.0f} {q[1]:7.0f} {q[2]:7.0f}")
    d = np.array([v for v in pm_delay if v is not None])
    print(f"polymarket   Up mid moved 1 c the same way within 3 s in {len(d)} of {len(pm_delay)} events with a book; "
          f"delay p25 / p50 / p75 {np.percentile(d, 25):.0f} / {np.percentile(d, 50):.0f} / {np.percentile(d, 75):.0f} ms")


if __name__ == "__main__":
    main()

"""Fill quality of a bot's live fills (and its paper twin's): is each fill worth more than we paid, a few seconds on?
Read-only: the bot's window logs + the recorder's books; places nothing, writes one JSON under data/probe/.

    nice -n 19 .venv/bin/python -m scripts.fill_report imb-maker-10 [--since "2026-10-27 01:36"] [--until ...]
                                                              [--scale-bot imb-maker-300]

Per fill (buy of `side` at p, q shares, at t = when the fill reached us):
  markout_h = mid of that token at t + h - p      (h = 5, 30, 60 s; mid = (best bid + best ask) / 2 from the recorder)
  rebate    = fee_rate x p x (1 - p) x rebate_share (the bot's own settings)
  edge_h    = markout_h + rebate                  per share: > 0 = bought below what it was worth
  settle    = (1 if that token won else 0) - p + rebate   (the noisy one: what the window really paid)
Means are per share; 95 % ranges by bootstrap over windows (fills in one window are not independent).
Breakdowns: price, side, join (top at the best bid) vs back (2 ticks behind), fills a guard pull followed within 2 s.
--scale-bot: that bot's paper shares per window over the same windows x the live edge = a rough $ / window and / day
at its size (assumes its fills would have the same edge per share).
"""
import argparse
import bisect
import collections
import glob
import gzip
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import orjson

from backend.bots.replay import _gz_lines
from backend.config import BOTS_DIR, DATA_DIR, RECORDINGS_DIR

HORIZONS = (5, 30, 60)


def load_windows(bot, since, until, live_only):
    out = []
    for f in sorted(glob.glob(str(BOTS_DIR / bot / "windows" / "*" / "*.json.gz"))):
        r = orjson.loads(gzip.decompress(Path(f).read_bytes()))
        if not since <= r["start"] < until:
            continue
        lv = r.get("live") or {}
        if live_only and lv.get("mode") != "live":
            continue
        out.append(r)
    return out


def scan_books(windows):
    """{slug: {asset: (times, mids, bid_times, bids)}} from the recordings, mid / best bid changes only."""
    need = {w["slug"]: (w["start"] * 1000.0 - 5000, w["end"] * 1000.0 + max(HORIZONS) * 1000 + 5000) for w in windows}
    hours = sorted({h for lo, hi in need.values() for h in range(int(lo // 3_600_000), int(hi // 3_600_000) + 1)})
    series = {s: collections.defaultdict(lambda: ([], [], [], [])) for s in need}
    best = collections.defaultdict(dict)                 # (slug, asset) -> {"b": bid, "a": ask}
    for h in hours:
        path = RECORDINGS_DIR / time.strftime("%Y-%m-%d/%H.tsv.gz", time.gmtime(h * 3600))
        if not path.exists():
            print("  (missing recording", path.name, "of", time.strftime("%Y-%m-%d", time.gmtime(h * 3600)) + ")")
            continue
        live = {s for s, (lo, hi) in need.items() if lo < (h + 1) * 3_600_000 and hi > h * 3_600_000}
        srcs = {"bk:" + s: s for s in live}
        t_end = max(need[s][1] for s in live)
        t0 = time.time()
        for line in _gz_lines(path):
            p = line.split("\t", 2)
            if len(p) < 3 or p[1] not in srcs:
                if len(p) == 3 and p[0][:1].isdigit() and float(p[0]) > t_end:
                    break
                continue
            slug, recv = srcs[p[1]], float(p[0])
            try:
                msgs = orjson.loads(p[2])
            except orjson.JSONDecodeError:
                continue
            for e in msgs if isinstance(msgs, list) else [msgs]:
                et = e.get("event_type")
                if et == "book":
                    bids = [float(x["price"]) for x in e.get("bids", [])]
                    asks = [float(x["price"]) for x in e.get("asks", [])]
                    upd = [(e.get("asset_id"), max(bids) if bids else None, min(asks) if asks else None)]
                elif et == "price_change":
                    upd = [(c.get("asset_id"), _f(c.get("best_bid")), _f(c.get("best_ask"))) for c in e.get("price_changes", [])]
                else:
                    continue
                for asset, b, a in upd:
                    if not asset:
                        continue
                    bk = best[(slug, asset)]
                    bk["b"], bk["a"] = b, a
                    ts, mids, bts, bs = series[slug][asset]
                    if b is not None and (not bs or bs[-1] != b):
                        bts.append(recv)
                        bs.append(b)
                    if b is not None and a is not None:
                        mid = (b + a) / 2
                        if not mids or mids[-1] != mid:
                            ts.append(recv)
                            mids.append(mid)
        print(f"  read {path.parent.name}/{path.name} in {time.time() - t0:.0f} s")
    return series


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def at(times, vals, t):
    i = bisect.bisect_right(times, t) - 1
    return vals[i] if i >= 0 else None


def token_of(w, ser):
    """{side: asset}: the asset whose best bid follows the decisions' Up best bid (bb) is Up."""
    dc = w["decisions"]["cols"]
    D = [dict(zip(dc, x)) for x in w["decisions"]["rows"] if x[1] is not None][::20]
    assets = list(ser)
    if len(assets) != 2 or not D:
        return None
    err = {a: sum(abs((at(ser[a][2], ser[a][3], d["ts_ms"]) or 0) * 1000 - d["bb"]) for d in D) for a in assets}
    up = min(assets, key=err.get)
    return {"up": up, "down": [a for a in assets if a != up][0]}


def fills_of(w, ser, tok, kind):
    """Fills of one layer ('live' or 'paper') with their markouts and labels."""
    cfg = w["cfg"]
    fee, share = cfg.get("fee_rate", 0.07), cfg.get("rebate_share", 0.2)
    dc = w["decisions"]["cols"]
    D = [dict(zip(dc, x)) for x in w["decisions"]["rows"]]
    dts = [d["ts_ms"] for d in D]
    gc = w["guard"]["cols"]
    guards = [dict(zip(gc, x)) for x in w["guard"]["rows"]]
    if kind == "live":
        oc = w["live"]["orders"]["cols"]
        orders = {x[oc.index("oid")]: dict(zip(oc, x)) for x in w["live"]["orders"]["rows"]}
        fc = w["live"]["fills"]["cols"]
        rows = [dict(zip(fc, x)) for x in w["live"]["fills"]["rows"]]
        oid_key, sent_key = "oid", "sent_ms"
    else:
        oc = w["orders"]["cols"]
        orders = {x[0]: dict(zip(oc, x)) for x in w["orders"]["rows"]}
        fc = w["fills"]["cols"]
        rows = [dict(zip(fc, x)) for x in w["fills"]["rows"]]
        oid_key, sent_key = "order", "sent_ms"
    out = []
    for r in rows:
        q, p, side, t = float(r["shares"]), float(r["price"]), r["side"], float(r["ts_ms"])
        if q <= 0:
            continue
        a = tok[side]
        ts, mids = ser[a][0], ser[a][1]
        reb = fee * p * (1 - p) * share
        f = {"slug": w["slug"], "kind": kind, "side": side, "p": p, "q": q, "t": t, "rebate": reb}
        for h in HORIZONS:
            m = at(ts, mids, t + h * 1000)
            f[f"edge{h}"] = None if m is None else m - p + reb
        f["settle"] = None if w.get("winner") not in ("up", "down") else (1.0 if w["winner"] == side else 0.0) - p + reb
        o = orders.get(r[oid_key]) or {}
        place = None
        if o.get(sent_key) is not None:
            i = bisect.bisect_right(dts, o[sent_key]) - 1
            if i >= 0 and D[i]["d_up"] is not None:
                place = "join" if (D[i]["d_up"] if side == "up" else D[i]["d_dn"]) == 0 else "back"
        f["place"] = place or "?"
        f["guard_after"] = any(g["side"] == side and t < g["ts_ms"] <= t + 2000 for g in guards)
        f["bucket"] = f"{int(p * 10) / 10:.1f}-{int(p * 10) / 10 + 0.1:.1f}"
        out.append(f)
    return out


def summarize(fills, key, n_boot=2000):
    """Per-share mean of `key`, with a 95 % range by bootstrap over windows."""
    F = [f for f in fills if f.get(key) is not None]
    if not F:
        return None
    byw = collections.defaultdict(lambda: [0.0, 0.0])
    for f in F:
        byw[f["slug"]][0] += f[key] * f["q"]
        byw[f["slug"]][1] += f["q"]
    W = list(byw.values())
    mean = sum(x for x, _ in W) / sum(q for _, q in W)
    rnd = random.Random(1)
    boots = []
    for _ in range(n_boot):
        s = [W[rnd.randrange(len(W))] for _ in W]
        boots.append(sum(x for x, _ in s) / sum(q for _, q in s))
    boots.sort()
    return {"mean_c": mean * 100, "lo_c": boots[int(0.025 * n_boot)] * 100, "hi_c": boots[int(0.975 * n_boot)] * 100,
            "fills": len(F), "shares": sum(f["q"] for f in F), "windows": len(W)}


def line(name, s):
    if not s:
        return f"  {name:30} -"
    return (f"  {name:30} {s['mean_c']:+6.2f} c/share  [{s['lo_c']:+6.2f} .. {s['hi_c']:+6.2f}]  "
            f"{s['fills']:4} fills {s['shares']:6.0f} sh {s['windows']:3} windows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bot")
    ap.add_argument("--since", default=None, help="UTC 'YYYY-MM-DD HH:MM' (window start, default: all)")
    ap.add_argument("--until", default=None)
    ap.add_argument("--scale-bot", default=None, help="a paper bot whose size to project the live edge to")
    a = ap.parse_args()
    ts = lambda s, d: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() if s else d
    since, until = ts(a.since, 0), ts(a.until, 1e12)
    windows = load_windows(a.bot, since, until, live_only=True)
    windows = [w for w in windows if (w["live"]["fills"]["rows"] or w["fills"]["rows"])]
    print(f"{a.bot}: {len(windows)} live windows with fills")
    if not windows:
        return
    series = scan_books(windows)
    fills = {"live": [], "paper": []}
    skipped = 0
    for w in windows:
        tok = token_of(w, series.get(w["slug"], {}))
        if tok is None:
            skipped += 1
            continue
        for kind in fills:
            fills[kind] += fills_of(w, series[w["slug"]], tok, kind)
    if skipped:
        print(f"  ({skipped} windows without recorded books skipped)")
    res = {"bot": a.bot, "since": since, "until": until, "windows": len(windows) - skipped}
    print("\nedge per share = price move after the fill + maker rebate (cents; 95 % range over windows)")
    for kind in ("live", "paper"):
        F = fills[kind]
        print(f"\n{kind.upper()} ({len(F)} fills)")
        res[kind] = {}
        for k in [f"edge{h}" for h in HORIZONS] + ["settle"]:
            s = summarize(F, k)
            res[kind][k] = s
            print(line({"settle": "settled (what windows paid)"}.get(k, f"after {k[4:]} s"), s))
        if kind == "live":
            print("  by price / side / placement / guard (edge after 30 s):")
            for label, fn in (("price", lambda f: f["bucket"]), ("side", lambda f: f["side"]),
                              ("placement", lambda f: f["place"]),
                              ("guard pull within 2 s", lambda f: "yes" if f["guard_after"] else "no")):
                for v in sorted({fn(f) for f in F}):
                    s = summarize([f for f in F if fn(f) == v], "edge30", 500)
                    res[kind][f"{label}={v}"] = s
                    print(line(f"{label} {v}", s))
    lv, pp = res["live"].get("edge30"), res["paper"].get("edge30")
    if lv and pp:
        print(f"\nlive vs paper twin after 30 s: {lv['mean_c'] - pp['mean_c']:+.2f} c/share "
              f"({lv['shares'] / max(len(windows) - skipped, 1):.1f} vs {pp['shares'] / max(len(windows) - skipped, 1):.1f} sh / window)")
    if a.scale_bot and lv:
        other = load_windows(a.scale_bot, since, until, live_only=False)
        slugs = {w["slug"] for w in windows}
        sh = [sum(float(x[4]) for x in w["fills"]["rows"] if float(x[4]) > 0) for w in other if w["slug"] in slugs]
        if sh:
            per = sum(sh) / len(sh)
            print(f"\nat {a.scale_bot}'s size ({per:.0f} sh / window over {len(sh)} windows): "
                  f"{per * lv['mean_c'] / 100:+.2f} $ / window, ~{per * lv['mean_c'] / 100 * 288:+.0f} $ / day "
                  f"[{per * lv['lo_c'] / 100 * 288:+.0f} .. {per * lv['hi_c'] / 100 * 288:+.0f}] if its fills had this edge")
            res["scale"] = {"bot": a.scale_bot, "shares_per_window": per}
    out = DATA_DIR / "probe" / time.strftime(f"fill_report-{a.bot}-%Y%m%d-%H%M%S.json", time.gmtime())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**res, "fills": fills}, default=str))
    print("\nsaved", out)


if __name__ == "__main__":
    main()

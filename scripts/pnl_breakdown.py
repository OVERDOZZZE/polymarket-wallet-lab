"""Where a bot's paper PnL comes from: settled $ per window split by ladder depth, price, time in the window, rebate
vs trading, guard pull after the fill. Read-only: the bot's window logs only (no recordings), writes nothing.

    nice -n 19 .venv/bin/python -m scripts.pnl_breakdown imb-maker-300 [--since "2026-10-26 00:00"] [--until ...]

Per paper fill (buy of `side` at p, q shares):
  trading = q x ((1 if that token won else 0) - p)     (held to settlement; a merge pays the same)
  rebate  = q x fee_rate x p x (1 - p) x rebate_share
  depth   = ticks (1 c) below that token's best bid when the order was sent (0 = at the top)
Each group's $ / window is its total over all settled windows / their count (windows without such fills count as 0),
with a 95 % range by bootstrap over windows. "cost / window" is what the group's fills cost (capital it ties up).
"""
import argparse
import bisect
import collections
import glob
import gzip
import random
from datetime import datetime, timezone
from pathlib import Path

import orjson

from backend.config import BOTS_DIR

DEPTHS = ((0, 1, "top"), (1, 3, "1-2 ticks"), (3, 6, "3-5 ticks"), (6, 13, "6-12 ticks"), (13, 999, "13+ ticks"))
PHASES = ((0, 60, "0-60 s"), (60, 120, "60-120 s"), (120, 180, "120-180 s"), (180, 300, "180-275 s"))  # [lo, hi)


def fills_of(w):
    cfg = w["cfg"]
    fee, share = cfg.get("fee_rate", 0.07), cfg.get("rebate_share", 0.2)
    dc = w["decisions"]["cols"]
    D = [dict(zip(dc, x)) for x in w["decisions"]["rows"]]
    dts = [d["ts_ms"] for d in D]
    gc = w["guard"]["cols"]
    guards = [dict(zip(gc, x)) for x in w["guard"]["rows"]]
    oc = w["orders"]["cols"]
    orders = {x[0]: dict(zip(oc, x)) for x in w["orders"]["rows"]}
    fc = w["fills"]["cols"]
    out = []
    for r in (dict(zip(fc, x)) for x in w["fills"]["rows"]):
        q, p, side, t = float(r["shares"]), float(r["price"]), r["side"], float(r["ts_ms"])
        if q <= 0:
            continue
        depth = None
        o = orders.get(r["order"]) or {}
        if o.get("sent_ms") is not None:
            i = bisect.bisect_right(dts, o["sent_ms"]) - 1
            if i >= 0 and D[i]["bb"] is not None and D[i]["ba"] is not None:
                top = D[i]["bb"] / 1000 if side == "up" else 1 - D[i]["ba"] / 1000
                depth = max(0, round((top - p) * 100))
        out.append({"q": q, "p": p, "side": side, "cost": q * p,
                    "trading": q * ((1.0 if w["winner"] == side else 0.0) - p),
                    "rebate": q * fee * p * (1 - p) * share,
                    "depth": depth, "sec": t / 1000 - w["start"],
                    "guard_after": any(g["side"] == side and t < g["ts_ms"] <= t + 2000 for g in guards)})
    return out


def label(v, table):
    if v is None:
        return "?"
    return next((name for lo, hi, name in table if lo <= v < hi), "?")


def boot(per_window, n=2000):
    """mean and 95 % range of a list of per-window $ values."""
    rnd = random.Random(1)
    m = sum(per_window) / len(per_window)
    b = sorted(sum(per_window[rnd.randrange(len(per_window))] for _ in per_window) / len(per_window) for _ in range(n))
    return m, b[int(0.025 * n)], b[int(0.975 * n)]


def report(name, groups, W):
    """groups: {label: {slug: (pnl, cost, shares, fills)}}"""
    print(f"\n{name:22} {'$ / window':>11} {'95 % range':>19} {'cost / win':>10} {'sh / win':>8} {'fills':>6}")
    for g in sorted(groups):
        per = groups[g]
        m, lo, hi = boot([per.get(s, (0.0,))[0] for s in W])
        cost = sum(v[1] for v in per.values()) / len(W)
        sh = sum(v[2] for v in per.values()) / len(W)
        n = sum(v[3] for v in per.values())
        print(f"  {g:20} {m:+11.3f}   [{lo:+7.3f} .. {hi:+7.3f}] {cost:10.2f} {sh:8.1f} {n:6}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bot")
    ap.add_argument("--since", default=None, help="UTC 'YYYY-MM-DD HH:MM' (window start, default: all)")
    ap.add_argument("--until", default=None)
    a = ap.parse_args()
    ts = lambda s, d: d if not s else datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
    since, until = ts(a.since, 0), ts(a.until, 1e12)
    keys = {"depth at placement": lambda f: label(f["depth"], DEPTHS),
            "price": lambda f: f"{int(f['p'] * 10) / 10:.1f}-{int(f['p'] * 10) / 10 + 0.1:.1f}",
            "time in window": lambda f: label(f["sec"], PHASES),
            "guard pull within 2 s": lambda f: "yes" if f["guard_after"] else "no"}
    groups = {k: collections.defaultdict(dict) for k in keys}
    tot = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
    W, starts = [], []
    for path in sorted(glob.glob(str(BOTS_DIR / a.bot / "windows" / "*" / "*.json.gz"))):
        w = orjson.loads(gzip.decompress(Path(path).read_bytes()))   # one window at a time: the logs are large
        if not since <= w["start"] < until or w.get("winner") not in ("up", "down"):
            continue
        W.append(w["slug"])
        starts.append(w["start"])
        tot[w["slug"]]
        for f in fills_of(w):
            pnl = f["trading"] + f["rebate"]
            for k, fn in keys.items():
                g = groups[k][fn(f)]
                p0 = g.get(w["slug"], (0.0, 0.0, 0.0, 0))
                g[w["slug"]] = (p0[0] + pnl, p0[1] + f["cost"], p0[2] + f["q"], p0[3] + 1)
            t = tot[w["slug"]]
            t[0] += f["trading"]
            t[1] += f["rebate"]
            t[2] += f["cost"]
    if not W:
        raise SystemExit("no settled windows")
    span = (starts[-1] - starts[0]) / 3600 + 5 / 60
    print(f"{a.bot}: {len(W)} settled windows, {datetime.fromtimestamp(starts[0], timezone.utc):%m-%d %H:%M}"
          f" .. {datetime.fromtimestamp(starts[-1], timezone.utc):%m-%d %H:%M} UTC ({span:.1f} h)")
    for i, name in ((0, "trading"), (1, "rebate")):
        m, lo, hi = boot([tot[s][i] for s in W])
        print(f"  {name:20} {m:+11.3f} $ / window  [{lo:+7.3f} .. {hi:+7.3f}]   total {sum(tot[s][i] for s in W):+.2f}")
    per = sorted(tot[s][0] + tot[s][1] for s in W)
    m, lo, hi = boot(per)
    print(f"  {'total':20} {m:+11.3f} $ / window  [{lo:+7.3f} .. {hi:+7.3f}]   total {sum(per):+.2f}")
    k = max(1, len(per) // 10)
    print(f"  windows won {sum(1 for x in per if x > 0)} / {len(per)}; best 10 % of windows {sum(per[-k:]):+.2f}, "
          f"worst 10 % {sum(per[:k]):+.2f}; cost / window {sum(tot[s][2] for s in W) / len(W):.2f}")
    for k in keys:
        report(k, groups[k], W)


if __name__ == "__main__":
    main()

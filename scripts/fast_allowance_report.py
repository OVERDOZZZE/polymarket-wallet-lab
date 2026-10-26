"""Table for a fast_allowance_sweep run: per variant fills, PnL, per-day PnL, and the PnL difference to a reference
variant with a bootstrap over windows (paired: same windows, same latency draws).

    .venv\\Scripts\\python scripts\\fast_allowance_report.py allowance [--ref '{"extra_pay": 0.0, "max_price": 0.6}']
"""
import argparse
import json
import random
import time
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "reports" / "fast_allowance"


def table(name, ref=None, sort="pnl", top=None):
    res = json.loads((OUT / f"{name}.json").read_text())
    days = sorted({time.strftime("%m-%d", time.gmtime(w["start"])) for r in res.values() for w in r["windows"]})
    if ref is None:
        ref = min(res, key=lambda n: len(n))
    ref_w = {w["slug"]: w["trading_pnl"] for w in res[ref]["windows"]}
    rows = []
    for n, r in res.items():
        o = r["orders"]
        filled = [x for x in o if x["result"] in ("filled", "partial")]
        cost = sum(w["cost"] for w in r["windows"])
        pnl = sum(w["trading_pnl"] for w in r["windows"])
        wins = sum(1 for w in r["windows"] if w["trading_pnl"] > 0)
        per_day = defaultdict(float)
        for w in r["windows"]:
            per_day[time.strftime("%m-%d", time.gmtime(w["start"]))] += w["trading_pnl"]
        mine = {w["slug"]: w["trading_pnl"] for w in r["windows"]}
        slugs = sorted(set(mine) | set(ref_w))
        diffs = [mine.get(s, 0.0) - ref_w.get(s, 0.0) for s in slugs]
        rng = random.Random(1)
        bs = sorted(sum(rng.choice(diffs) for _ in diffs) for _ in range(1000)) if diffs else [0.0] * 1000
        sh = sum(x["filled"] or 0 for x in filled)
        avg = sum((x["avg"] or 0) * (x["filled"] or 0) for x in filled) / sh if sh else 0.0
        rows.append({"name": n, "orders": len(o), "fills": len(filled), "fill_rate": len(filled) / len(o) if o else 0,
                     "cost": cost, "pnl": pnl, "roi": pnl / cost if cost else 0, "windows": len(r["windows"]),
                     "win%": wins / len(r["windows"]) if r["windows"] else 0, "avg": avg,
                     "days": [per_day.get(d, 0.0) for d in days], "d": sum(diffs), "lo": bs[25], "hi": bs[975]})
    rows.sort(key=lambda x: -x[sort])
    print(f"reference: {ref}")
    print(f"{'variant':58s} {'ord':>4s} {'fill%':>6s} {'$cost':>7s} {'pnl':>8s} {'roi':>7s} {'avg':>5s} {'win%':>5s} "
          + " ".join(f"{d:>7s}" for d in days) + "   vs ref [95%]")
    for x in rows[:top]:
        print(f"{x['name'][:58]:58s} {x['orders']:4d} {x['fill_rate']:6.1%} {x['cost']:7.0f} {x['pnl']:+8.1f} "
              f"{x['roi']:+7.1%} {x['avg']:5.2f} {x['win%']:5.1%} " + " ".join(f"{v:+7.0f}" for v in x["days"])
              + f"   {x['d']:+6.0f} [{x['lo']:+.0f}, {x['hi']:+.0f}]")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--ref")
    ap.add_argument("--sort", default="pnl")
    ap.add_argument("--top", type=int)
    a = ap.parse_args()
    ref = json.dumps(json.loads(a.ref), sort_keys=True) if a.ref else None
    table(a.name, ref, a.sort, a.top)

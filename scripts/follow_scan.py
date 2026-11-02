"""Could we follow a studied trader? (LONDON_PLAN research option 3.) Read-only, on trader_forensics' fills files.

    python -m scripts.follow_scan [who ...]            # default: machinelearned crouch hcrystallash wzbbb

For every BUY of the trader (maker and taker apart): a follower sees it D seconds after the match (on-chain
OrderFilled / data-api; ~3-5 s at best) and buys the same token as a taker at the mid then + half a tick, paying the
taker fee, held to settlement (won = 1). Also the 30 s exit (mid 30 s later). Cents per share, per day and pooled.
"""
import collections
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import DATA_DIR  # noqa: E402

DAYS = ["2026-10-25", "2026-10-26", "2026-10-27"]
FEE = 0.07


def main():
    who = sys.argv[1:] or ["machinelearned", "crouch", "hcrystallash", "wzbbb"]
    rows = collections.defaultdict(list)
    for d in DAYS:
        f = DATA_DIR / "forensics" / f"fills-{d}.jsonl.gz"
        if not f.exists():
            continue
        for x in gzip.open(f, "rt"):
            r = json.loads(x)
            if r["who"] in who and r["side"] == "BUY" and r.get("won") is not None and "-5m-" in r["slug"]:
                rows[(r["who"], r["taker"])].append((d, r))
    print("who            role   n      their c/sh | follow at D s (mid_D + 0.5c + fee), to settlement | 30 s exit")
    for (w, tk), rs in sorted(rows.items()):
        own = [100 * ((1.0 if r["won"] else 0.0) - r["price"]) for _, r in rs]
        out = []
        for key in ("mid1", "mid5", "mid10"):
            v = [100 * ((1.0 if r["won"] else 0.0) - (r[key] + 0.005) - FEE * r[key] * (1 - r[key]))
                 for _, r in rs if r.get(key) is not None and 0.02 < r[key] < 0.98]
            out.append(f"{key[3:]:>2}s {sum(v) / max(1, len(v)):+6.2f}")
        ex = [100 * (r["mid30"] - (r["mid5"] + 0.005) - FEE * r["mid5"] * (1 - r["mid5"]))
              for _, r in rs if r.get("mid5") is not None and r.get("mid30") is not None and 0.02 < r["mid5"] < 0.98]
        print(f"{w:14s} {'taker' if tk else 'maker'} {len(rs):6d} {sum(own) / len(own):+8.2f}  | "
              + "  ".join(out) + f" | 5->30 s {sum(ex) / max(1, len(ex)):+6.2f}")
        per = collections.defaultdict(list)
        for d, r in rs:
            if r.get("mid5") is not None and 0.02 < r["mid5"] < 0.98:
                per[d].append(100 * ((1.0 if r["won"] else 0.0) - (r["mid5"] + 0.005) - FEE * r["mid5"] * (1 - r["mid5"])))
        print("                     follow 5 s by day: " + ", ".join(f"{d[5:]} {sum(v) / len(v):+.2f} ({len(v)})"
                                                         for d, v in sorted(per.items())))


if __name__ == "__main__":
    main()

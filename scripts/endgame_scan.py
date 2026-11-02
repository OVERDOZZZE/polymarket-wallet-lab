"""BTC 5m endgame: in a window's last seconds most of the settlement TWAP is already fixed. Is the near-certain
winner still for sale below 1, how much, and does it really win? (read-only, places no orders)

    python -m scripts.endgame_scan extract 2026-10-24 [--dir data/recordings_server]
    python -m scripts.endgame_scan report data/forensics/endgame-2026-10-24.jsonl.gz [more...]

extract: for every 5m window with its strike (our Chainlink TWAP at the start, markets.twap_of), every 0.5 s from
end - 40 s to end + 5 s, using only what had been RECEIVED by then: the Chainlink ticks of the settlement TWAP
(observation seconds [end-62, end-3]) received so far, Binance futures (receipt time) + basis to Chainlink, realized
futures vol; the projected TWAP (known ticks + spot for the rest) and its sd (random walk, k x vol, the rest
seconds), z = (projection - strike) / sd; the predicted winner's best ask and the shares offered at <= 0.95 / 0.97 /
0.98 / 0.99, and the loser's best bid. Plus the official winner. Token -> outcome from data/forensics/api-<day>.
report: by z and seconds to the end: how often the predicted side won, the ask, the depth, and the profit per share
of a taker buy (1 - ask - fee, fee = 0.07 x p x (1 - p)) with its worst cases.
"""
import argparse
import bisect
import collections
import gzip
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.config import DATA_DIR, RECORDINGS_DIR  # noqa: E402
from backend.polymarket.markets import twap_of, twap_secs  # noqa: E402

OUT = DATA_DIR / "forensics"
FEE = 0.07
STEPS = [x / 2 for x in range(-80, 11)]        # seconds relative to the end, every 0.5 s


def _tokens(day):
    m = {}
    with gzip.open(OUT / f"api-{day}.jsonl.gz", "rt") as fh:
        for x in fh:
            r = json.loads(x)
            if "-5m-" in r["slug"]:
                m[r["asset"]] = (r["slug"], (r["outcome"] or "").lower())
    return m


def extract(day, rec_dir):
    tok = _tokens(day)
    s, e = replay._ts(f"{day} 00:00"), replay._ts(f"{day} 00:00") + 86400
    cl, cl_recv = {}, {}                          # observation second -> value / receipt ms
    bf_r, bf_p = [], []                           # futures by receipt ms
    basis = None
    books = collections.defaultdict(lambda: {"bids": {}, "asks": {}})   # asset -> price -> size
    due, seen, rows = [], set(), []

    def bf_at(t):
        i = bisect.bisect_right(bf_r, t) - 1
        return bf_p[i] if i >= 0 else None

    def vol(t, span=300):
        xs = [bf_at(t - k * 1000) for k in range(span + 1)]
        rs = [math.log(xs[i] / xs[i + 1]) for i in range(span) if xs[i] and xs[i + 1]]
        return 1e4 * math.sqrt(sum(r * r for r in rs) / len(rs)) if len(rs) > span / 2 else None

    def side(asset):
        b = books.get(asset)
        if not b:
            return None
        bid = max((p for p, q in b["bids"].items() if q > 0), default=None)
        asks = sorted((p, q) for p, q in b["asks"].items() if q > 0)
        return bid, asks

    for ms, src, raw in replay.iter_recording(s - 400, e + 600, rec_dir):
        if src == "cl":
            try:
                p = json.loads(raw)["payload"]
            except (ValueError, KeyError):
                continue
            for x in p["data"] if "data" in p else [p]:
                if "timestamp" not in x or "value" not in x:
                    continue
                sec = int(x["timestamp"]) // 1000
                cl[sec] = float(x["value"])
                cl_recv.setdefault(sec, ms)
                fb = bf_at(ms - 1500)
                if fb and "data" not in p:
                    b = float(x["value"]) - fb
                    basis = b if basis is None else basis + 0.05 * (b - basis)
        elif src == "bf":
            try:
                bf_p.append(float(json.loads(raw)["p"]))
            except (ValueError, KeyError):
                continue
            bf_r.append(ms)
            if len(bf_r) > 300000:
                k = bisect.bisect_left(bf_r, bf_r[-1] - 1200000)
                del bf_r[:k], bf_p[:k]
        elif src.startswith("bk:btc-updown-5m-"):
            slug = src[3:]
            st = int(slug.rsplit("-", 1)[1])
            if slug not in seen and s <= st < e:
                seen.add(slug)
                for dt in STEPS:
                    bisect.insort(due, (1000.0 * (st + 300 + dt), slug, dt))
            try:
                js = json.loads(raw)
            except ValueError:
                continue
            for m in js if isinstance(js, list) else [js]:
                if "price_changes" in m:
                    for pc in m["price_changes"]:
                        bk = books[pc["asset_id"]]["bids" if pc["side"] == "BUY" else "asks"]
                        bk[float(pc["price"])] = float(pc["size"])
                elif m.get("event_type") == "book":
                    a = m["asset_id"]
                    books[a] = {"bids": {float(x["price"]): float(x["size"]) for x in m.get("bids") or []},
                                "asks": {float(x["price"]): float(x["size"]) for x in m.get("asks") or []}}
        while due and due[0][0] <= ms:
            t_ms, slug, dt = due.pop(0)
            st = int(slug.rsplit("-", 1)[1])
            end = st + 300
            K = twap_of(cl, st)
            fb = bf_at(t_ms)
            v = vol(t_ms)
            if K is None or fb is None or basis is None or v is None:
                continue
            secs = list(twap_secs(end))
            known = [cl[x] for x in secs if x in cl and cl_recv.get(x, 9e18) <= t_ms]
            S = fb + basis
            rest = len(secs) - len(known)
            # the unknown seconds: those not yet observed follow a random walk from S; seconds already observed but
            # not yet received are ~S as well (their spread is small, 1-2 s)
            future = [x for x in secs if x not in cl or cl_recv.get(x, 9e18) > t_ms]
            mean = (sum(known) + rest * S) / len(secs)
            sig = max(v, 0.05) * 1e-4 * S
            ahead = [max(0.0, x - t_ms / 1000.0) for x in future]               # seconds ahead of now per missing tick
            # Var of the mean of a random walk sampled at those times: sigma^2 / n^2 * sum_ij min(t_i, t_j)
            ahead.sort()
            n = len(secs)
            var = sum((2 * (len(ahead) - i) - 1) * a for i, a in enumerate(ahead)) * sig * sig / (n * n)
            sd = math.sqrt(max(var, 1e-9))
            z = (mean - K) / sd
            pred = "up" if mean >= K else "down"
            assets = {o: a for a, (sl, o) in tok.items() if sl == slug}
            if "up" not in assets or "down" not in assets:
                continue
            w, lo = side(assets[pred]), side(assets["down" if pred == "up" else "up"])
            if w is None:
                continue
            asks = w[1]
            depth = {th: sum(q for p, q in asks if p <= th + 1e-9) for th in (0.95, 0.97, 0.98, 0.99)}
            rows.append({"slug": slug, "dt": dt, "K": K, "mean": mean, "sd": sd, "z": z, "pred": pred, "n_known": len(known),
                         "ask": asks[0][0] if asks else None, "ask_sz": asks[0][1] if asks else None,
                         "d95": depth[0.95], "d97": depth[0.97], "d98": depth[0.98], "d99": depth[0.99],
                         "lose_bid": lo[0] if lo else None})
    win = replay.winners_for({r["slug"] for r in rows})
    rows = [dict(r, won=(str(win[r["slug"]]).lower() == r["pred"])) for r in rows if win.get(r["slug"])]
    f = OUT / f"endgame-{day}.jsonl.gz"
    with gzip.open(f, "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"{len(rows)} samples in {len({r['slug'] for r in rows})} windows -> {f}")


def report(files):
    rows = []
    for f in files:
        with gzip.open(f, "rt") as fh:
            rows += [json.loads(x) for x in fh]
    print(f"{len(rows)} samples, {len({r['slug'] for r in rows})} windows")
    zb = ((0, 2), (2, 3), (3, 4), (4, 5), (5, 7), (7, 10), (10, 1e9))
    tb = ((-40, -20), (-20, -10), (-10, -5), (-5, -3), (-3, 0), (0, 5.1))
    print("\nwon% of the predicted side | median ask | share of samples with an ask <= 0.98 | mean shares <= 0.98")
    print(f"{'|z|':>9s} " + " ".join(f"{f'{a}..{b}s':>24s}" for a, b in tb))
    for z0, z1 in zb:
        line = []
        for t0, t1 in tb:
            xs = [r for r in rows if z0 <= abs(r["z"]) < z1 and t0 <= r["dt"] < t1]
            if len(xs) < 5:
                line.append(f"{'-':>24s}")
                continue
            won = 100 * sum(r["won"] for r in xs) / len(xs)
            asks = sorted(r["ask"] for r in xs if r["ask"] is not None)
            cheap = [r for r in xs if r["ask"] is not None and r["ask"] <= 0.98]
            line.append(f"{won:5.1f}% {asks[len(asks) // 2] if asks else float('nan'):.3f} "
                        f"{100 * len(cheap) / len(xs):4.0f}% {sum(r['d98'] for r in xs) / len(xs):6.0f}")
        print(f"{z0:>4}-{z1 if z1 < 1e8 else 'inf':<4} " + " ".join(line))
    print("\nTaker buy of the predicted side at its best ask (when <= cap), first such moment per window, by z threshold:")
    for zmin in (3, 4, 5, 7, 10):
        for cap in (0.97, 0.98, 0.99):
            for t0 in (-20, -10, -5):
                first = {}
                for r in sorted(rows, key=lambda r: r["dt"]):
                    if r["slug"] in first or r["dt"] < t0 or r["dt"] > 0 or abs(r["z"]) < zmin:
                        continue
                    if r["ask"] is not None and r["ask"] <= cap:
                        first[r["slug"]] = r
                xs = list(first.values())
                if not xs:
                    continue
                pnl = [((1.0 if r["won"] else 0.0) - r["ask"] - FEE * r["ask"] * (1 - r["ask"])) for r in xs]
                sz = [min(r["ask_sz"] or 0, 1e9) for r in xs]
                lost = sum(1 for r in xs if not r["won"])
                print(f"  |z|>={zmin:<3} ask<={cap} from end{t0:+d}s: {len(xs):4d} windows, lost {lost:3d}, avg ask "
                      f"{sum(r['ask'] for r in xs) / len(xs):.3f}, profit {100 * sum(pnl) / len(pnl):+5.2f} c/sh, "
                      f"median shares at the ask {sorted(sz)[len(sz) // 2]:7.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["extract", "report"])
    ap.add_argument("args", nargs="+")
    ap.add_argument("--dir", default=None)
    a = ap.parse_args()
    if a.cmd == "extract":
        extract(a.args[0], a.dir or RECORDINGS_DIR)
    else:
        report(a.args)


if __name__ == "__main__":
    main()

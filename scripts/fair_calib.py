"""Is a BTC fair-value model better than the Polymarket price? Calibration of P(Up) for BTC 5m windows (read-only).

    python -m scripts.fair_calib extract 2026-10-24 [--dir data/recordings_server]
    python -m scripts.fair_calib fit data/forensics/calib-2026-10-24.jsonl.gz [more...]

extract: every 5m window with a full Chainlink record gets samples at fixed seconds into the window (SAMPLES): strike K
(our 60 s TWAP of the Chainlink ticks at the start, markets.twap_of), the TWAP seconds already observed, the last
Chainlink tick, Binance futures (exchange time) + its basis to Chainlink (EWMA of tick - futures at the tick's
observation time), realized futures vol (bps per sqrt s) over the last 60 s and 300 s, the Up token's book mid, the
official winner. Token -> outcome from data/forensics/api-<day> (scripts/trader_forensics fetch).
fit: P = Phi((E[TWAP] - K) / sd) with sd from vol x k (the engine's TWAP model, market_maker._model); k fitted by
log-loss; compared with the market mid and a logistic blend of both (does the model add anything to the price?).
"""
import argparse
import bisect
import collections
import gzip
import json
import math
import sys
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.config import DATA_DIR, RECORDINGS_DIR  # noqa: E402
from backend.polymarket.markets import twap_of, twap_secs  # noqa: E402

SAMPLES = (10, 20, 30, 45, 60, 90, 120, 150, 180, 210, 240, 255, 270, 280, 290)
OUT = DATA_DIR / "forensics"
_N = NormalDist()


def _outcomes(day):
    m = {}
    with gzip.open(OUT / f"api-{day}.jsonl.gz", "rt") as fh:
        for x in fh:
            r = json.loads(x)
            if "-5m-" in r["slug"]:
                m[r["asset"]] = (r["slug"], (r["outcome"] or "").lower())
    return m


def extract(day, rec_dir):
    tok = _outcomes(day)
    s, e = replay._ts(f"{day} 00:00"), replay._ts(f"{day} 00:00") + 86400
    cl = {}                                    # observation second -> Chainlink value
    bf_t, bf_p = [], []                        # futures trades (exchange ms, price), last 20 min
    basis = None
    up_mid = {}                                # slug -> (bid, ask) of the Up token, latest
    due = []                                   # (receipt ms, slug, t_in) samples still to take
    starts = set()
    rows = []

    def bf_at(t):
        i = bisect.bisect_right(bf_t, t) - 1
        return bf_p[i] if i >= 0 else None

    def vol(t, span):
        """bps per sqrt(s) from 1 s futures returns over the last `span` s."""
        xs = [bf_at(t - k * 1000) for k in range(span + 1)]
        rs = [math.log(xs[i] / xs[i + 1]) for i in range(span) if xs[i] and xs[i + 1]]
        return 1e4 * math.sqrt(sum(r * r for r in rs) / len(rs)) if len(rs) > span / 2 else None

    for ms, src, raw in replay.iter_recording(s - 400, e, rec_dir):
        if src == "cl":
            try:
                p = json.loads(raw)["payload"]
            except (ValueError, KeyError):
                continue
            for x in p["data"] if "data" in p else [p]:          # a (re)subscribe sends a batch of past ticks
                if "timestamp" not in x or "value" not in x:
                    continue
                cl[int(x["timestamp"]) // 1000] = float(x["value"])
                fb = bf_at(x["timestamp"])
                if fb and "data" not in p:
                    b = float(x["value"]) - fb
                    basis = b if basis is None else basis + 0.05 * (b - basis)
        elif src == "bf":
            try:
                js = json.loads(raw)
            except ValueError:
                continue
            bf_t.append(float(js["T"]))
            bf_p.append(float(js["p"]))
            if len(bf_t) > 300000:
                k = bisect.bisect_left(bf_t, bf_t[-1] - 1200000)
                del bf_t[:k], bf_p[:k]
        elif src.startswith("bk:btc-updown-5m-"):
            slug = src[3:]
            st = int(slug.rsplit("-", 1)[1])
            if st not in starts and s <= st < e:
                starts.add(st)
                for t_in in SAMPLES:
                    bisect.insort(due, (1000.0 * (st + t_in), slug, t_in))
            try:
                js = json.loads(raw)
            except ValueError:
                continue
            for m in js if isinstance(js, list) else [js]:
                if "price_changes" in m:
                    for pc in m["price_changes"]:
                        if tok.get(pc["asset_id"], (None, None))[1] == "up" and pc.get("best_bid") is not None:
                            up_mid[slug] = (float(pc["best_bid"]), float(pc["best_ask"] or 1))
                elif m.get("event_type") == "book" and tok.get(m.get("asset_id"), (None, None))[1] == "up":
                    b = max((float(x["price"]) for x in m.get("bids") or []), default=0.0)
                    a = min((float(x["price"]) for x in m.get("asks") or []), default=1.0)
                    up_mid[slug] = (b, a)
        while due and due[0][0] <= ms:
            t_ms, slug, t_in = due.pop(0)
            st = int(slug.rsplit("-", 1)[1])
            K = twap_of(cl, st)
            ba = up_mid.get(slug)
            fb = bf_at(t_ms)
            if K is None or ba is None or fb is None or basis is None:
                continue
            end = st + 300
            secs = list(twap_secs(end))
            obs = [cl[x] for x in secs if x in cl and x <= t_ms / 1000 - 1.5]     # ticks reach us ~1.5 s late
            last_cl = max((x for x in cl if x <= t_ms / 1000), default=None)
            rows.append({"slug": slug, "t_in": t_in, "K": K, "S": fb + basis, "cl": cl.get(last_cl) if last_cl else None,
                         "n_obs": len(obs), "obs_mean": sum(obs) / len(obs) if obs else None,
                         "tau_twap": max(0.0, secs[-1] - t_ms / 1000.0), "v60": vol(t_ms, 60), "v300": vol(t_ms, 300),
                         "bid": ba[0], "ask": ba[1]})
    win = replay.winners_for({r["slug"] for r in rows})
    rows = [dict(r, up_won=(str(win[r["slug"]]).lower() == "up")) for r in rows if win.get(r["slug"])]
    f = OUT / f"calib-{day}.jsonl.gz"
    with gzip.open(f, "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"{len(rows)} samples in {len({r['slug'] for r in rows})} windows -> {f}")


def model(r, k, vkey="v300", floor=0.05):
    """P(Up): TWAP over [end-62, end-3]; random walk from S with sd = k x vol."""
    v = r.get(vkey) or r.get("v300") or r.get("v60")
    if v is None:
        return None
    sig = max(v, floor) * 1e-4 * r["S"] * k                    # $ per sqrt(s)
    W, tau = 60.0, r["tau_twap"]
    if tau >= W:
        mean, var = r["S"], sig * sig * (tau - W + W / 3.0)
    else:
        known = r["obs_mean"] if r["obs_mean"] is not None else r["S"]
        mean = ((W - tau) * known + tau * r["S"]) / W
        var = (tau / W) ** 2 * sig * sig * tau / 3.0
    return min(max(_N.cdf((mean - r["K"]) / math.sqrt(max(var, 1e-12))), 0.002), 0.998)


def _ll(ps, ys):
    return -sum(math.log(p if y else 1 - p) for p, y in zip(ps, ys)) / len(ys)


def _logit(p):
    p = min(max(p, 0.002), 0.998)
    return math.log(p / (1 - p))


def _fit_blend(xa, xb, ys, iters=400):
    """logit P = a * logit(mid) + b * logit(model) + c by gradient descent on log-loss."""
    a, b, c, lr = 1.0, 0.0, 0.0, 0.05
    for _ in range(iters):
        ga = gb = gc = 0.0
        for u, v, y in zip(xa, xb, ys):
            p = 1 / (1 + math.exp(-(a * u + b * v + c)))
            g = p - y
            ga += g * u
            gb += g * v
            gc += g
        n = len(ys)
        a, b, c = a - lr * ga / n, b - lr * gb / n, c - lr * gc / n
    return a, b, c


def fit(files):
    rows = []
    for f in files:
        with gzip.open(f, "rt") as fh:
            rows += [json.loads(x) for x in fh]
    rows = [r for r in rows if r["ask"] - r["bid"] <= 0.05 and 0 < r["bid"] < r["ask"] < 1]
    print(f"{len(rows)} samples, {len({r['slug'] for r in rows})} windows")
    ys = [1 if r["up_won"] else 0 for r in rows]
    mid = [(r["bid"] + r["ask"]) / 2 for r in rows]
    print(f"  market mid           log-loss {_ll(mid, ys):.4f}")
    best = None
    for vkey in ("v60", "v300"):
        for k in (0.15, 0.25, 0.35, 0.45, 0.6, 0.8, 1.0, 1.5):
            ps = [model(r, k, vkey) for r in rows]
            ok = [(p, y) for p, y in zip(ps, ys) if p is not None]
            ll = _ll([p for p, _ in ok], [y for _, y in ok])
            print(f"  model {vkey} k={k:<4}   log-loss {ll:.4f} ({len(ok)} samples)")
            if best is None or ll < best[0]:
                best = (ll, vkey, k)
    _, vkey, k = best
    ps = [model(r, k, vkey) for r in rows]
    idx = [i for i, p in enumerate(ps) if p is not None]
    a, b, c = _fit_blend([_logit(mid[i]) for i in idx], [_logit(ps[i]) for i in idx], [ys[i] for i in idx])
    bl = [1 / (1 + math.exp(-(a * _logit(mid[i]) + b * _logit(ps[i]) + c))) for i in idx]
    print(f"  best model {vkey} k={k}; blend logit = {a:.3f} x mid + {b:.3f} x model + {c:.3f}: log-loss "
          f"{_ll(bl, [ys[i] for i in idx]):.4f} vs mid {_ll([mid[i] for i in idx], [ys[i] for i in idx]):.4f}")
    print("\n  by seconds into the window: log-loss mid / model / blend, and mean |model - mid|")
    by = collections.defaultdict(list)
    for j, i in enumerate(idx):
        by[rows[i]["t_in"]].append((mid[i], ps[i], bl[j], ys[i]))
    for t in sorted(by):
        xs = by[t]
        print(f"   {t:3d}s n {len(xs):4d}  mid {_ll([x[0] for x in xs], [x[3] for x in xs]):.4f}  model "
              f"{_ll([x[1] for x in xs], [x[3] for x in xs]):.4f}  blend {_ll([x[2] for x in xs], [x[3] for x in xs]):.4f}"
              f"  |d| {100 * sum(abs(x[1] - x[0]) for x in xs) / len(xs):5.2f} c")
    print("\n  when the model disagrees with the mid by >= d cents: share of samples, and how often the model side wins")
    for d in (2, 5, 10):
        sel = [(mid[i], ps[i], ys[i]) for i in idx if abs(ps[i] - mid[i]) >= d / 100]
        if sel:
            right = sum(1 for m_, p_, y in sel if (p_ > m_) == (y == 1))
            gain = sum(((y - m_) if p_ > m_ else (m_ - y)) for m_, p_, y in sel) / len(sel)
            print(f"   >= {d:2d} c: {100 * len(sel) / len(idx):5.1f}% of samples, model side right {100 * right / len(sel):5.1f}%, "
                  f"buying the model's side at mid earns {100 * gain:+.2f} c/share")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["extract", "fit"])
    ap.add_argument("args", nargs="+")
    ap.add_argument("--dir", default=None)
    a = ap.parse_args()
    if a.cmd == "extract":
        extract(a.args[0], a.dir or RECORDINGS_DIR)
    else:
        fit(a.args)


if __name__ == "__main__":
    main()

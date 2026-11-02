"""Can a maker see a sweep of its price level coming? (LONDON_PLAN / trader_forensics: our bids fill mostly when the
whole best-bid level is taken, and those fills lose; fills that leave the level standing earn ~+1 c.) Read-only.

    python -m scripts.sweep_predict extract 2026-10-24 [--dir data/recordings_server]
    python -m scripts.sweep_predict fit data/forensics/sweep-2026-10-24.npz [...] --test data/forensics/sweep-2026-11-01.npz
    python -m scripts.sweep_predict export [hit_model.pkl bid_value_hit.json]   # regressor -> backend/bots/*.json

extract: every 200 ms (receipt time) of each BTC 5m window (5 s .. 275 s in), for both tokens, the state of the
token's best bid level and the label "the level is gone (best bid below it) at some moment 50..1000 ms later".
Features: size at the best bid / the next level / the 3 below, best ask size, spread, queue share, the level's age,
its size change over 0.5 s and 2 s, trades on the token over 0.5 s / 2 s, book messages of the window over 0.5 s,
Binance futures return over 0.5 s / 2 s aligned to the token (+ = good for its holders), price, seconds into the
window. Also the token mid 1 s and 10 s later (the cost of a fill there).
fit: gradient boosting on the train days, scored on the test day: AUC, and for cancel thresholds the share of sweeps
caught vs the share of quoting time given up.
"""
import argparse
import bisect
import collections
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.config import DATA_DIR, RECORDINGS_DIR  # noqa: E402

OUT = DATA_DIR / "forensics"
FEATS = ["q1", "q2", "q3", "a1", "spread", "share", "age_ms", "dq500", "dq2000", "tr500", "tr2000", "msg500",
         "btc500", "btc2000", "price", "t_in"]
STEP_MS, H0, H1 = 200, 50, 1000


def _tokens(day):
    m = {}
    import gzip
    with gzip.open(OUT / f"api-{day}.jsonl.gz", "rt") as fh:
        for x in fh:
            r = json.loads(x)
            if "-5m-" in r["slug"]:
                m[r["asset"]] = (r["slug"], (r["outcome"] or "").lower())
    return m


class Tok:
    __slots__ = ("bids", "asks", "best", "best_since", "hist", "trades", "pending", "hpend")

    def __init__(self):
        self.bids, self.asks = {}, {}
        self.best, self.best_since = None, 0.0
        self.hist = collections.deque()          # (ms, best bid, size at it)
        self.trades = collections.deque()        # (ms, size)
        self.pending = []                        # [due_ms, best price, row index]
        self.hpend = []                          # [from ms, to ms, best price, row index]: prints into the level

    def bb(self):
        return max((p for p, q in self.bids.items() if q > 0), default=None)

    def ba(self):
        return min((p for p, q in self.asks.items() if q > 0), default=None)


def extract(day, rec_dir, h0=0, h1=24):
    try:
        tok = _tokens(day)
    except FileNotFoundError:                                # no data-api file: the recording's info lines
        tok = {}
    by_start = collections.defaultdict(list)                 # window start -> [(asset, slug, outcome)]
    for a, (sl, o) in tok.items():
        by_start[int(sl.rsplit("-", 1)[1])].append((a, sl, o))
    other = {}                                               # asset -> the other token of its market
    for st, lst in by_start.items():
        if len(lst) == 2:
            other[lst[0][0]], other[lst[1][0]] = lst[1][0], lst[0][0]
    HIT = []                                                 # per row: shares printed into the level in 50..1000 ms

    def hit(t, ms, cond, sz):
        keep = []
        for f, u, b, i in t.hpend:
            if u < ms:
                continue
            if f < ms and cond(b):
                HIT[i] += sz
            keep.append((f, u, b, i))
        t.hpend = keep
    s, e = replay._ts(f"{day} 00:00") + 3600 * h0, replay._ts(f"{day} 00:00") + 3600 * h1
    T = collections.defaultdict(Tok)
    bf_r, bf_p = [], []
    msgs = collections.defaultdict(collections.deque)       # slug -> book message receipt times
    X, Y, MID, META = [], [], [], []
    future_mid = []                                          # (due ms, row, asset, which)
    next_sample = 0.0

    def bf_at(t):
        i = bisect.bisect_right(bf_r, t) - 1
        return bf_p[i] if i >= 0 else None

    def mid_of(t):
        b, a = t.bb(), t.ba()
        return (b + a) / 2 if b is not None and a is not None else None

    for ms, src, raw in replay.iter_recording(s, e, rec_dir):
        if src == "bf":
            try:
                bf_p.append(float(json.loads(raw)["p"]))
            except (ValueError, KeyError):
                continue
            bf_r.append(ms)
            if len(bf_r) > 200000:
                k = bisect.bisect_left(bf_r, ms - 600000)
                del bf_r[:k], bf_p[:k]
            continue
        if src == "info":
            try:
                inf = json.loads(raw)
            except ValueError:
                continue
            sl = inf.get("slug", "")
            if "-5m-" in sl and sl.startswith("btc-") and inf.get("tokens"):
                tk = inf["tokens"]
                if tk.get("up") not in tok and tk.get("down") not in tok:
                    st0 = int(sl.rsplit("-", 1)[1])
                    for o, a in tk.items():
                        tok[a] = (sl, o)
                        by_start[st0].append((a, sl, o))
                    other[tk["up"]], other[tk["down"]] = tk["down"], tk["up"]
            continue
        if not src.startswith("bk:btc-updown-5m-"):
            continue
        slug = src[3:]
        try:
            js = json.loads(raw)
        except ValueError:
            continue
        q = msgs[slug]
        q.append(ms)
        while q and q[0] < ms - 500:
            q.popleft()
        touched = set()
        for m in js if isinstance(js, list) else [js]:
            if "price_changes" in m:
                for pc in m["price_changes"]:
                    t = T[pc["asset_id"]]
                    (t.bids if pc["side"] == "BUY" else t.asks)[float(pc["price"])] = float(pc["size"])
                    touched.add(pc["asset_id"])
            elif m.get("event_type") == "book":
                t = T[m["asset_id"]]
                t.bids = {float(x["price"]): float(x["size"]) for x in m.get("bids") or []}
                t.asks = {float(x["price"]): float(x["size"]) for x in m.get("asks") or []}
                touched.add(m["asset_id"])
            elif m.get("event_type") == "last_trade_price":
                a, sz = m["asset_id"], float(m.get("size") or 0)
                T[a].trades.append((ms, sz))
                px, sd = float(m.get("price") or 0), str(m.get("side") or "").upper()
                if sd == "SELL":                              # a taker sold into this token's bids
                    hit(T[a], ms, lambda b: px <= b + 1e-9, sz)
                elif sd == "BUY" and a in other:              # a taker bought the other token: mint vs these bids
                    hit(T[other[a]], ms, lambda b: px >= 1 - b - 1e-9, sz)
        for a in touched:
            t = T[a]
            b = t.bb()
            if b != t.best:
                t.best, t.best_since = b, ms
            t.hist.append((ms, b, t.bids.get(b, 0.0) if b is not None else 0.0))
            while t.hist and t.hist[0][0] < ms - 2500:
                t.hist.popleft()
            while t.trades and t.trades[0][0] < ms - 2000:
                t.trades.popleft()
            keep = []
            for due, p, i in t.pending:                       # label: level gone in (t+50, t+1000]
                if Y[i] == 1:
                    continue
                if ms > due + H1 - H0:
                    continue                                  # horizon over, stays 0
                if ms >= due and (b is None or b < p - 1e-9):
                    Y[i] = 1
                    continue
                keep.append([due, p, i])
            t.pending = keep
        while future_mid and future_mid[0][0] <= ms:
            _, i, a, k = future_mid.pop(0)
            MID[i][k] = mid_of(T[a]) if a in T else None
        if ms < next_sample:
            continue
        next_sample = ms + STEP_MS
        cur = int(ms / 1000) // 300 * 300
        for a, sl, outc in by_start.get(cur, ()):
            st = cur
            t_in = ms / 1000 - st
            if not 5 <= t_in <= 275 or a not in T:
                continue
            t = T[a]
            b, k = t.bb(), t.ba()
            if b is None or k is None or not 0.05 <= b <= 0.95:
                continue
            q1 = t.bids.get(b, 0.0)
            q2 = t.bids.get(round(b - 0.01, 2), 0.0)
            q3 = sum(t.bids.get(round(b - 0.01 * j, 2), 0.0) for j in (1, 2, 3))
            a1 = t.asks.get(k, 0.0)

            def size_ago(dt):
                for hm, hb, hq in reversed(t.hist):
                    if hm <= ms - dt:
                        return hq if hb == b else 0.0
                return q1
            f500, f2000 = bf_at(ms), bf_at(ms - 500)
            g2000 = bf_at(ms - 2000)
            sgn = 1 if outc == "up" else -1
            btc500 = sgn * 1e4 * (f500 / f2000 - 1) if f500 and f2000 else 0.0
            btc2000 = sgn * 1e4 * (f500 / g2000 - 1) if f500 and g2000 else 0.0
            X.append([q1, q2, q3, a1, round((k - b) * 100), q1 / (q1 + a1) if q1 + a1 > 0 else 0.5,
                      min(ms - t.best_since, 60000), q1 - size_ago(500), q1 - size_ago(2000),
                      sum(z for tm, z in t.trades if tm >= ms - 500), sum(z for tm, z in t.trades),
                      len(msgs[sl]), btc500, btc2000, b, t_in])
            Y.append(0)
            HIT.append(0.0)
            MID.append([(b + k) / 2, None, None])
            META.append(st)
            i = len(Y) - 1
            t.pending.append([ms + H0, b, i])
            t.hpend.append((ms + H0, ms + H1, b, i))
            if len(t.hpend) > 64:
                t.hpend = [z for z in t.hpend if z[1] >= ms]
            bisect.insort(future_mid, (ms + 1000, i, a, 1))
            bisect.insort(future_mid, (ms + 10000, i, a, 2))
    mid = np.array([[np.nan if v is None else v for v in r] for r in MID], dtype=float)
    f = OUT / (f"sweep-{day}.npz" if (h0, h1) == (0, 24) else f"sweep-{day}-{h0:02d}-{h1:02d}.npz")
    np.savez_compressed(f, X=np.array(X, dtype=float), y=np.array(Y, dtype=np.int8), mid=mid, win=np.array(META),
                        hit=np.array(HIT, dtype=float))
    print(f"{len(Y)} samples, sweep rate {np.mean(Y):.3f}, hit rate {np.mean(np.array(HIT) > 0):.3f} -> {f}")


def fit(train, test):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    def load(fs):
        d = [np.load(f) for f in fs]
        return np.vstack([x["X"] for x in d]), np.concatenate([x["y"] for x in d]), np.vstack([x["mid"] for x in d])
    Xtr, ytr, _ = load(train)
    Xte, yte, mte = load(test)
    print(f"train {len(ytr)} (sweep {ytr.mean():.3f}), test {len(yte)} (sweep {yte.mean():.3f})")
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=31)
    clf.fit(Xtr, ytr)
    import pickle
    with open(OUT / "sweep_model.pkl", "wb") as fh:            # used by imb_london_replay's -swNN variants
        pickle.dump({"model": clf, "feats": FEATS, "train": [str(f) for f in train]}, fh)
    p = clf.predict_proba(Xte)[:, 1]
    print(f"test AUC {roc_auc_score(yte, p):.3f}")
    # how bad is a bid-side fill when the level is swept vs not: mid move 1 s / 10 s later vs the bid price
    bid = Xte[:, FEATS.index("price")]
    for lab, sel in (("swept", yte == 1), ("not swept", yte == 0)):
        d1 = np.nanmean(mte[sel, 1] - bid[sel]) * 100
        d10 = np.nanmean(mte[sel, 2] - bid[sel]) * 100
        print(f"  {lab:10s}: mid 1 s later - bid {d1:+.2f} c, 10 s later {d10:+.2f} c")
    print("  cancel when P(sweep) >= thr: sweeps caught | quoting time given up | precision")
    for thr in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        c = p >= thr
        print(f"   thr {thr:.1f}: caught {100 * (c & (yte == 1)).sum() / max(1, (yte == 1).sum()):5.1f}% | "
              f"out {100 * c.mean():5.1f}% | precision {100 * (c & (yte == 1)).sum() / max(1, c.sum()):5.1f}%")
    imp = sorted(zip(FEATS, _perm_importance(clf, Xte[:20000], yte[:20000])), key=lambda x: -x[1])
    print("  permutation importance (AUC drop):", ", ".join(f"{k} {v:.3f}" for k, v in imp[:8]))


def export(src="sweep_model.pkl", name=""):
    """A pickled regressor ({"reg": ...} in data/forensics/<src>) -> config.BID_VALUE_MODEL, or the file `name` next to
    it (JSON trees, read by imb_maker's value guard without sklearn; its value_model setting picks the file). Node =
    [feature, threshold, left, right, missing goes left, is leaf, value]; x <= threshold goes left (sklearn's rule)."""
    import pickle
    from backend.config import BID_VALUE_MODEL
    if name:
        BID_VALUE_MODEL = BID_VALUE_MODEL.parent / name
    with open(OUT / src, "rb") as fh:
        obj = pickle.load(fh)
    reg = obj["reg"]
    trees = [[[int(n["feature_idx"]), float(n["num_threshold"]), int(n["left"]), int(n["right"]),
               int(n["missing_go_to_left"]), int(n["is_leaf"]), float(n["value"])] for n in it[0].nodes]
             for it in reg._predictors]
    assert not any(n["is_categorical"] for it in reg._predictors for n in it[0].nodes)
    out = {"feats": FEATS, "base": float(np.ravel(reg._baseline_prediction)[0]), "trees": trees,
           "train": obj.get("train"), "target": "token mid 10 s later - best bid, cents"}
    BID_VALUE_MODEL.write_text(json.dumps(out, separators=(",", ":")))
    X = np.load(OUT / "sweep-2026-11-01.npz")["X"][:2000]
    from backend.bots.strategies.imb_maker import BidValueModel
    m = BidValueModel(BID_VALUE_MODEL)
    err = max(abs(m.predict(list(r)) - p) for r, p in zip(X, reg.predict(X)))
    print(f"{len(trees)} trees -> {BID_VALUE_MODEL} ({BID_VALUE_MODEL.stat().st_size // 1024} KB), max |diff| vs sklearn "
          f"{err:.2e} on 2000 samples")


def _perm_importance(clf, X, y):
    from sklearn.metrics import roc_auc_score
    base = roc_auc_score(y, clf.predict_proba(X)[:, 1])
    rng = np.random.default_rng(0)
    out = []
    for j in range(X.shape[1]):
        Xp = X.copy()
        Xp[:, j] = rng.permutation(Xp[:, j])
        out.append(base - roc_auc_score(y, clf.predict_proba(Xp)[:, 1]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["extract", "fit", "export"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--test", nargs="+", default=None)
    ap.add_argument("--from", dest="h0", type=int, default=0)
    ap.add_argument("--to", dest="h1", type=int, default=24)
    a = ap.parse_args()
    if a.cmd == "extract":
        extract(a.args[0], a.dir or RECORDINGS_DIR, a.h0, a.h1)
    elif a.cmd == "export":
        export(*a.args)
    else:
        fit(a.args, a.test)


if __name__ == "__main__":
    main()

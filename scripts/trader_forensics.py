"""Fill-by-fill comparison of the profitable traders' BTC Up/Down fills with ours (read-only, places no orders).

    python -m scripts.trader_forensics fetch 2026-11-01 [--from 8 --to 20]      # needs internet (data-api)
    python -m scripts.trader_forensics extract 2026-11-01 [--dir data/recordings] [--from 8 --to 20]
    python -m scripts.trader_forensics report data/forensics/fills-2026-11-01.jsonl.gz [more files...]

fetch: every BTC Up/Down fill of the watched wallets (WALLETS, plus our trading wallet as "us", taken from the local
server's /api/trade/status or the PM_US environment variable, never written out) from data-api v2 activity (TRADE
rows); role from the taker-only trades list (same tx + token + size = taker, else maker) -> data/forensics/api-<day>.
extract: one pass over the day's recordings. Each fill gets its match time (the CLOB last_trade_price event with the
same transaction hash, else data-api's block second - 1 s), the token's best bid / ask 1 s and 250 ms before, its
mid 1 / 5 / 10 / 30 s after and at the window's end, and Binance futures returns (exchange trade time) over 2 s and
500 ms before the match and 10 s after -> data/forensics/fills-<day>.jsonl.gz. (The recorded RTDS `act` stream is not
used: it misses whole hours.)
report: per wallet x role x market x side: fills, shares, $ volume, markout at 1/5/10/30 s and at settlement (cents
per share, size-weighted, + = good for the wallet), the book's move just before the fill, BTC moves aligned to the
position.
"""
import argparse
import bisect
import collections
import gzip
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.config import DATA_DIR, PORT, RECORDINGS_DIR  # noqa: E402

WALLETS = {"0x21f4c1a1be34a39a7bc20985df4b7e5a39fb691d": "crouch", "0x86b14772a4da70f3e6d5eee06933ebef143efc87": "machinelearned",
           "0x576b0696fd5a9225d66fd9500fd98f5be10b0cab": "hcrystallash", "0x19b3823092892c026517fd0ff58df4889abe0b9b": "wzbbb"}
HORIZONS = (1000, 5000, 10000, 30000)
PATH_MS = range(-5000, 2001, 100)
OUT_DIR = DATA_DIR / "forensics"
API = "https://data-api.polymarket.com/v2"


def _our_wallet():
    w = os.environ.get("PM_US", "")
    if not w:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/trade/status", timeout=5) as r:
                w = json.load(r).get("wallet") or ""
        except Exception:
            w = ""
    return w.lower()


def _day_range(day, h0, h1):
    d0 = replay._ts(f"{day} 00:00")
    return d0 + 3600 * h0, d0 + 3600 * h1


class Hist:
    """Time series of one value, lookups at or before a time."""
    __slots__ = ("t", "v")

    def __init__(self):
        self.t, self.v = [], []

    def add(self, t, v):
        if self.v and self.v[-1] == v:
            return
        self.t.append(t)
        self.v.append(v)

    def at(self, t):
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else None

    def trim(self, before):
        i = bisect.bisect_left(self.t, before)
        if i > 1000:
            del self.t[:i - 1], self.v[:i - 1]


def _end_ms(slug):
    try:
        start = int(slug.rsplit("-", 1)[1])
    except (ValueError, AttributeError, IndexError):
        return None
    return 1000 * (start + (900 if "-15m-" in slug else 300 if "-5m-" in slug else 3600))


def _pages(url, params):
    import requests
    out, cur = [], None
    while True:
        q = {**params, **({"cursor": cur} if cur else {})}
        for i in range(4):
            try:
                js = requests.get(url, params=q, timeout=30).json()
                break
            except Exception:
                time.sleep(1 + i)
        else:
            raise RuntimeError(f"{url} failed")
        out += js.get("data") or []
        cur = (js.get("pagination") or {}).get("next_cursor")
        if not cur or not js.get("data"):
            return out


def fetch(day, h0, h1):
    us = _our_wallet()
    who = {**WALLETS, **({us: "us"} if us else {})}
    s, e = _day_range(day, h0, h1)
    rows = []
    for w, name in who.items():
        acts = _pages(f"{API}/activity", {"user": w, "type": "TRADE", "start": int(s), "end": int(e) - 1, "limit": 1000})
        tk = _pages(f"{API}/trades", {"user": w, "takerOnly": "true", "start": int(s), "end": int(e) - 1, "limit": 1000})
        taker = collections.Counter((x["transaction_hash"], x["token_id"], round(float(x["size"]), 4)) for x in tk)
        for x in acts:
            if not (x.get("slug") or "").startswith("btc-updown-"):
                continue
            k = (x["transaction_hash"], x["token_id"], round(float(x["size"]), 4))
            is_t = taker[k] > 0
            if is_t:
                taker[k] -= 1
            rows.append({"who": name, "t_api": x["timestamp"], "tx": x["transaction_hash"], "asset": x["token_id"],
                         "slug": x["slug"], "outcome": x.get("outcome"), "side": x["side"], "price": float(x["price"]),
                         "size": float(x["size"]), "taker": is_t})
        print(f"  {name}: {len(acts)} trades, {len(tk)} taker", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    f = OUT_DIR / f"api-{day}.jsonl.gz"
    with gzip.open(f, "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"{len(rows)} BTC fills -> {f}")


def _finalize(rows, books, btc, ltp):
    """Match time and markouts of the fills of one finished window (its book history is complete)."""
    for r in rows:
        t = ltp.get(r["tx"])
        r["t_src"] = "ltp" if t else "api"
        r["t"] = t = t or r["t_api"] * 1000.0 - 1000.0
        bk = books.get(r["asset"])
        r["ba_1s"] = bk.at(t - 1000) if bk else None
        r["ba_250"] = bk.at(t - 250) if bk else None
        r["ba_p300"] = bk.at(t + 300) if bk else None              # level exhausted? (best bid below the fill price)
        r["ba_p1"] = bk.at(t + 1000) if bk else None
        for h in HORIZONS:
            ba = bk.at(t + h) if bk else None
            r[f"mid{h // 1000}"] = (ba[0] + ba[1]) / 2 if ba else None
        ba = bk.at(_end_ms(r["slug"]) - 1) if bk else None
        r["mid_end"] = (ba[0] + ba[1]) / 2 if ba else None
        p0 = btc.at(t)
        for k, dt in (("ret2s", -2000), ("ret500", -500), ("fwd10", 10000)):
            pb = btc.at(t + dt)
            r[k] = (1e4 * math.log(p0 / pb) if dt < 0 else 1e4 * math.log(pb / p0)) if p0 and pb else None
        # BTC path around the match: bps vs the price at the match, every 100 ms from -5 s to +2 s (PATH_MS)
        r["path"] = [round(1e4 * math.log(btc.at(t + dt) / p0), 2) if p0 and btc.at(t + dt) else None for dt in PATH_MS]


def _book_of(books, slug_assets, slug, a):
    if a not in books:
        books[a] = Hist()
        slug_assets[slug].add(a)
    return books[a]


def extract(day, rec_dir, h0, h1):
    with gzip.open(OUT_DIR / f"api-{day}.jsonl.gz", "rt") as fh:
        api = [json.loads(x) for x in fh]
    s, e = _day_range(day, h0, h1)
    open_rows = collections.defaultdict(list)                 # slug -> fills waiting for the window to end
    for r in api:
        if s <= r["t_api"] < e:
            open_rows[r["slug"]].append(r)
    print(f"{sum(map(len, open_rows.values()))} fills in {len(open_rows)} windows", flush=True)
    books, slug_assets = {}, collections.defaultdict(set)   # asset -> Hist of (bid, ask) at recv time
    btc, ltp, done = Hist(), {}, []                           # Binance futures at exchange time; tx -> match ms
    n, t0, last_sweep = 0, time.time(), 0.0
    for ms, src, raw in replay.iter_recording(s - 120, e + 960, rec_dir):
        n += 1
        if src.startswith("bk:"):
            try:
                js = json.loads(raw)
            except ValueError:
                continue
            slug = src[3:]
            for m in js if isinstance(js, list) else [js]:
                if "price_changes" in m:
                    for pc in m["price_changes"]:
                        if pc.get("best_bid") is not None:
                            _book_of(books, slug_assets, slug, pc["asset_id"]).add(
                                ms, (float(pc["best_bid"]), float(pc["best_ask"] or 1)))
                elif m.get("event_type") == "book":
                    b = max((float(x["price"]) for x in m.get("bids") or []), default=0.0)
                    k = min((float(x["price"]) for x in m.get("asks") or []), default=1.0)
                    _book_of(books, slug_assets, slug, m["asset_id"]).add(ms, (b, k))
                elif m.get("event_type") == "last_trade_price" and m.get("transaction_hash"):
                    tx, ts = m["transaction_hash"], float(m.get("timestamp") or ms)
                    if tx not in ltp or ts < ltp[tx]:
                        ltp[tx] = ts
        elif src == "bf":
            try:
                js = json.loads(raw)
                btc.add(float(js["T"]), float(js["p"]))
            except (ValueError, KeyError):
                pass
        if ms - last_sweep > 10000:                          # windows over for 60 s: finalize and forget
            last_sweep = ms
            for slug in [x for x in set(slug_assets) | set(open_rows) if (_end_ms(x) or 9e15) + 60000 < ms]:
                rs = open_rows.pop(slug, [])
                _finalize(rs, books, btc, ltp)
                done += rs
                for a in slug_assets.pop(slug, ()):
                    books.pop(a, None)
            btc.trim(ms - 1800000)
            if len(ltp) > 400000:
                ltp = {k: v for k, v in ltp.items() if v > ms - 1800000}
        if n % 5000000 == 0:
            print(f"  {n / 1e6:.0f}M lines, {len(done)} fills done, {time.time() - t0:.0f} s", flush=True)
    for slug, rs in open_rows.items():
        _finalize(rs, books, btc, ltp)
        done += rs
    winners = replay.winners_for({r["slug"] for r in done})
    for r in done:
        wn = winners.get(r["slug"])
        r["won"] = None if not wn else (str(wn).lower() == str(r["outcome"]).lower())
    f = OUT_DIR / f"fills-{day}.jsonl.gz"
    with gzip.open(f, "wt") as fh:
        for r in done:
            fh.write(json.dumps(r) + "\n")
    print(f"{len(done)} fills ({sum(r['t_src'] == 'ltp' for r in done)} timed by tx) -> {f} ({n / 1e6:.1f}M lines, "
          f"{time.time() - t0:.0f} s)")


def _mid(ba):
    return (ba[0] + ba[1]) / 2 if ba else None


def report(files):
    rows = []
    for f in files:
        with gzip.open(f, "rt") as fh:
            rows += [json.loads(x) for x in fh]
    g = collections.defaultdict(list)
    for r in rows:
        if r.get("mid1") is None:                         # no book recorded for it (e.g. a gap)
            continue
        mkt = "15m" if "-15m-" in r["slug"] else "5m" if "-5m-" in r["slug"] else "other"
        g[(r["who"], "taker" if r["taker"] else "maker", mkt, r["side"])].append(r)
    cols = ["fills", "shares", "usd", "avg_px", "mk1", "mk5", "mk10", "mk30", "settle", "pre", "btc2s", "btc500", "btcfwd"]
    print(f"{'wallet':15s} {'role':5s} {'mkt':4s} {'side':4s} " + " ".join(f"{c:>8s}" for c in cols))
    for k in sorted(g):
        rs = g[k]

        def wavg(fn):
            num = den = 0.0
            for r in rs:
                v = fn(r)
                if v is not None:
                    num += v * r["size"]
                    den += r["size"]
            return num / den if den else float("nan")

        d = lambda r: 1 if r["side"] == "BUY" else -1
        al = lambda r: d(r) * (1 if (r["outcome"] or "").lower() == "up" else -1)    # + = BTC up favours the position
        mk = lambda h: (lambda r: d(r) * (r[f"mid{h}"] - r["price"]) * 100 if r.get(f"mid{h}") is not None else None)
        vals = [len(rs), sum(r["size"] for r in rs), sum(r["size"] * r["price"] for r in rs), wavg(lambda r: r["price"]),
                wavg(mk(1)), wavg(mk(5)), wavg(mk(10)), wavg(mk(30)),
                wavg(lambda r: d(r) * ((1.0 if r["won"] else 0.0) - r["price"]) * 100 if r.get("won") is not None else None),
                wavg(lambda r: d(r) * (_mid(r["ba_250"]) - _mid(r["ba_1s"])) * 100 if r["ba_250"] and r["ba_1s"] else None),
                wavg(lambda r: al(r) * r["ret2s"] if r.get("ret2s") is not None else None),
                wavg(lambda r: al(r) * r["ret500"] if r.get("ret500") is not None else None),
                wavg(lambda r: al(r) * r["fwd10"] if r.get("fwd10") is not None else None)]
        print(f"{k[0]:15s} {k[1]:5s} {k[2]:4s} {k[3]:4s} " + " ".join(
            f"{v:8d}" if isinstance(v, int) else f"{v:8.2f}" for v in vals))
    print("\nmk / settle / pre: cents per share, size-weighted, + = good for the wallet (pre = the book mid's move from "
          "t-1 s to t-250 ms in the fill's direction); btc*: Binance futures bps aligned to the position (2 s / 500 ms "
          "before the match, 10 s after)")


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def deep(files):
    """Takers: how long after a BTC move their fills match. Makers: fills split by the BTC move in the second before
    (against the position = picked off), with markouts per bucket."""
    rows = []
    for f in files:
        with gzip.open(f, "rt") as fh:
            rows += [json.loads(x) for x in fh]
    rows = [r for r in rows if r.get("mid1") is not None and r.get("path") and r["side"] == "BUY" and r["t_src"] == "ltp"]
    def aligned_path(r):
        al = 1 if (r["outcome"] or "").lower() == "up" else -1
        return [None if v is None else al * v for v in r["path"]]   # BTC vs the match, + = in the position's favour

    def onset(r, thr=1.0):
        """ms since BTC first moved >= thr bps in the position's favour (last 5 s), else None."""
        ap = aligned_path(r)
        for i, lag in sorted(((i, -dt) for i, dt in enumerate(PATH_MS) if dt < 0), key=lambda x: x[1]):
            if ap[i] is not None and ap[i] <= -thr:                       # BTC was thr bps worse (aligned) then
                return lag
        return None

    by = collections.defaultdict(list)
    for r in rows:
        by[(r["who"], "taker" if r["taker"] else "maker")].append(r)
    mk = lambda r, h: (r[f"mid{h}"] - r["price"]) * 100 if r.get(f"mid{h}") is not None else None

    def wavg(rs, fn):
        num = den = 0.0
        for r in rs:
            v = fn(r)
            if v is not None:
                num += v * r["size"]
                den += r["size"]
        return num / den if den else float("nan")

    print("TAKERS: BTC (Binance futures, aligned to the bought side) before the match; lag = ms since BTC was 1 bp "
          "lower (aligned), within 5 s")
    print(f"{'wallet':15s} {'fills':>6s} {'move%':>6s} {'lag p25':>8s} {'p50':>6s} {'p75':>6s} {'-2s':>6s} {'-1s':>6s} "
          f"{'-0.5s':>6s} {'-0.2s':>6s} {'+0.5s':>6s} {'+2s':>6s} {'mk1':>6s} {'mk10':>6s} {'mk1 <500ms':>10s} {'mk1 >500':>9s}")
    idx = {dt: i for i, dt in enumerate(PATH_MS)}
    for (who, role), rs in sorted(by.items()):
        if role != "taker":
            continue
        lags = [(onset(r), r) for r in rs]
        mv = [x for x in lags if x[0] is not None]
        prof = [wavg(rs, lambda r, i=idx[dt]: aligned_path(r)[i])
                for dt in (-2000, -1000, -500, -200, 500, 2000)]
        fast = [r for l, r in mv if l <= 500]
        slow = [r for l, r in mv if l > 500]
        print(f"{who:15s} {len(rs):6d} {100 * len(mv) / len(rs):6.1f} {_pct([l for l, _ in mv], .25):8.0f} "
              f"{_pct([l for l, _ in mv], .5):6.0f} {_pct([l for l, _ in mv], .75):6.0f} "
              + " ".join(f"{v:6.2f}" for v in prof)
              + f" {wavg(rs, lambda r: mk(r, 1)):6.2f} {wavg(rs, lambda r: mk(r, 10)):6.2f} "
                f"{wavg(fast, lambda r: mk(r, 1)):10.2f} {wavg(slow, lambda r: mk(r, 1)):9.2f}")
    print("  (-2s..+2s: BTC at that time vs at the match, bps aligned to the position: - before = BTC rose into the "
          "fill, + after = kept rising)")

    print("\nMAKERS: fills by BTC's aligned move over the second before the match (<= -1 bp = BTC moved against the bid)")
    print(f"{'wallet':15s} {'fills':>6s} {'against%':>8s} {'mk1 calm':>9s} {'mk1 agst':>9s} {'mk10 calm':>9s} "
          f"{'mk10 agst':>9s} {'settle calm':>11s} {'settle agst':>11s} {'sh/fill':>8s}")
    st = lambda r: ((1.0 if r["won"] else 0.0) - r["price"]) * 100 if r.get("won") is not None else None
    for (who, role), rs in sorted(by.items()):
        if role != "maker":
            continue
        i1 = idx[-1000]
        mv1 = lambda r: aligned_path(r)[i1]
        ag = [r for r in rs if mv1(r) is not None and mv1(r) >= 1.0]        # BTC 1 s earlier was >= 1 bp better
        calm = [r for r in rs if mv1(r) is not None and mv1(r) < 1.0]
        W = sum(r["size"] for r in rs)
        print(f"{who:15s} {len(rs):6d} {100 * sum(r['size'] for r in ag) / W:8.1f} {wavg(calm, lambda r: mk(r, 1)):9.2f} "
              f"{wavg(ag, lambda r: mk(r, 1)):9.2f} {wavg(calm, lambda r: mk(r, 10)):9.2f} {wavg(ag, lambda r: mk(r, 10)):9.2f} "
              f"{wavg(calm, st):11.2f} {wavg(ag, st):11.2f} {W / len(rs):8.1f}")
    print("  (against% = share of shares filled right after BTC moved >= 1 bp against the bid; markouts in cents/share)")

    print("\nMAKERS by queue outcome: 'swept' = the best bid 300 ms after the fill is below the fill price (the whole "
          "level was taken: a back-of-queue order fills only then); 'deep' = the fill price was below the best bid "
          "250 ms before (a ladder level reached by a sweep)")
    print(f"{'wallet':15s} {'fills':>6s} {'swept%':>7s} {'deep%':>6s} {'mk1 kept':>9s} {'mk1 swept':>9s} {'mk10 kept':>9s} "
          f"{'mk10 swpt':>9s} {'settle kept':>11s} {'settle swpt':>11s} {'mk10 deep':>9s}")
    for (who, role), rs in sorted(by.items()):
        if role != "maker":
            continue
        rs = [r for r in rs if r.get("ba_p300") and r.get("ba_250")]
        if not rs:
            continue
        sw = [r for r in rs if r["ba_p300"][0] < r["price"] - 1e-9]
        kept = [r for r in rs if r["ba_p300"][0] >= r["price"] - 1e-9]
        dp = [r for r in rs if r["price"] < r["ba_250"][0] - 1e-9]
        W = sum(r["size"] for r in rs)
        print(f"{who:15s} {len(rs):6d} {100 * sum(r['size'] for r in sw) / W:7.1f} {100 * sum(r['size'] for r in dp) / W:6.1f} "
              f"{wavg(kept, lambda r: mk(r, 1)):9.2f} {wavg(sw, lambda r: mk(r, 1)):9.2f} {wavg(kept, lambda r: mk(r, 10)):9.2f} "
              f"{wavg(sw, lambda r: mk(r, 10)):9.2f} {wavg(kept, st):11.2f} {wavg(sw, st):11.2f} {wavg(dp, lambda r: mk(r, 10)):9.2f}")

    print("\nOUR MAKER FILLS vs the others' fills in the same transaction (same taker order):")
    tx_of = collections.defaultdict(list)
    for r in rows:
        tx_of[r["tx"]].append(r)
    ours = [r for r in rows if r["who"] == "us" and not r["taker"]]
    shared = collections.Counter()
    for r in ours:
        for o in tx_of[r["tx"]]:
            if o["who"] != "us":
                shared[o["who"]] += 1
    print(f"  our maker fills {len(ours)}; fills by watched wallets in the same tx: {dict(shared)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "extract", "report", "deep"])
    ap.add_argument("args", nargs="+")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--from", dest="h0", type=int, default=0)
    ap.add_argument("--to", dest="h1", type=int, default=24)
    a = ap.parse_args()
    if a.cmd == "fetch":
        fetch(a.args[0], a.h0, a.h1)
    elif a.cmd == "extract":
        extract(a.args[0], a.dir or RECORDINGS_DIR, a.h0, a.h1)
    elif a.cmd == "deep":
        deep(a.args)
    else:
        report(a.args)


if __name__ == "__main__":
    main()

"""Convert the old btc5m logger's database (the home PC that ran the first bots, 2026-10-19..22; see
data/import/btc5m/INVENTORY.md) into backend/recorder.py's format, so backend/bots/replay.py can run any bot
over those ~64 hours:

    .venv\\Scripts\\python scripts\\import_btc5m.py [--timing home|dublin] [--hours N]
    .venv\\Scripts\\python -m backend.bots.replay --dir data/import/btc5m/recordings_dublin --hours 64 ...

Output: data/import/btc5m/recordings_<timing>/<YYYY-MM-DD>/<HH>.tsv.gz, plus the official winners of every
validated window merged into data/cache/resolutions.json (replay scores on those).

What is converted (source tag -> message shaped like the live feed the engine parses):
  * prices coinbase -> cb ticker (price = last trade; the old logger kept no best bid/ask, so both = price);
    kraken -> kr ticker (trade-triggered; `volume` = row id so every row counts as a trade);
    chainlink_spot -> cl RTDS update; ext_ticks binance_spot_bbo -> bn; binance_fut_trade -> bf as aggTrade (what the
    live bots read; --fut bbo: binance_fut_bbo as bookTicker, into recordings_<timing>_futbbo);
  * windows (validation = match) -> info, 30 s before the start;
  * book_snap (top 10 levels: every book event + 1/s) and book_top (every best-price change, size-only changes
    at most every 250 ms) -> one reconstructed top-of-book per token, emitted as a CLOB `book` event the first time
    and `price_change` deltas after; trades -> `last_trade_price`. Levels deeper than 10 are not known.

Clock: the logger stamped receive times with the home PC's clock, which was off by up to ~1 s at times. Every
time is corrected exactly as the old analysis did (analysis/build_panel.py: clock_corrector): true = recv -
(median Coinbase receive-minus-source offset of that minute - 34 ms).

Timing (--timing):
  home   = as received at home (Coinbase ~34 ms, Kraken ~70 ms, Polymarket books ~68 ms after the source).
  dublin = re-timed to the Dublin server's measured medians (Coinbase 49 ms, Kraken 20 ms, books 12 ms; measured
           2026-10-22 from its recordings): each feed is shifted by the difference of medians, never earlier than
           a Dublin p10 after its source time, keeping each stream's order. Delays that came from the source side
           (Polymarket's own queueing in spikes) stay in. Chainlink and Binance are left as received (Binance
           latency from Dublin is not measured yet).
"""
import argparse
import bisect
import gzip
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.config import IMPORT_DIR                     # noqa: E402

SRC = IMPORT_DIR / "btc5m"
DB = SRC / "raw_db" / "btc5m.db"
CB_TRUE_LAT = 34
MARGIN_MS = 5000
LEVELS = 10
# feed -> (shift ms, floor after source ms) for --timing dublin (home medians from the old analysis)
DUBLIN = {"cb": (49 - 34, 45), "kr": (20 - 70, 8), "bk": (12 - 68, 9)}


def clock_fix(con):
    rows = con.execute("SELECT ts, detail FROM health WHERE event='clock_offset' AND feed='coinbase' ORDER BY ts").fetchall()
    ts = [r[0] for r in rows]
    err = [int(re.search(r"median=(-?\d+)", r[1]).group(1)) - CB_TRUE_LAT for r in rows]

    def fix(recv):
        i = min(bisect.bisect_left(ts, recv), len(ts) - 1)     # row stamped at the end of its minute
        return recv - err[i]
    return fix


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Timer:
    """Output time per stream: clock-corrected, optionally re-timed to Dublin, never going backwards."""

    def __init__(self, timing):
        self.timing, self.last = timing, {}

    def __call__(self, stream, feed, t, src):
        if self.timing == "dublin" and feed in DUBLIN and src:
            shift, floor = DUBLIN[feed]
            t = max(t + shift, src + floor)
        t = max(t, self.last.get(stream, t))
        self.last[stream] = t
        return t


def lv(levels):
    return {round(float(p), 4): float(s) for p, s in levels}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--timing", choices=("home", "dublin"), default="dublin")
    ap.add_argument("--hours", type=float, default=None, help="only the first N hours (testing)")
    ap.add_argument("--fut", choices=("trade", "bbo"), default="trade",
                    help="Binance futures as aggTrade (what the live bots use since 2026-10-22) or bookTicker")
    a = ap.parse_args()
    out_dir = SRC / (f"recordings_{a.timing}" + ("" if a.fut == "trade" else "_futbbo"))
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    fix = clock_fix(con)
    timer = Timer(a.timing)

    wins = con.execute("SELECT slug, start, end, condition_id, up_token, down_token, fee_schedule, min_size, tick, outcome "
                       "FROM windows WHERE validation = 'match' ORDER BY start").fetchall()
    tok_id = {t: i for i, t in con.execute("SELECT id, token FROM tokens")}
    by_id = {}                                   # token int id -> (slug, token string)
    infos = []
    for slug, start, end, cond, up, dn, fees, min_size, tick, outcome in wins:
        fs = json.loads(fees or "{}")
        info = {"slug": slug, "tokens": {"up": up, "down": dn}, "conditionId": cond, "tick": tick or 0.01,
                "min_size": min_size or 5, "twap": True, "twap_s": 60, "fee_rate": fs.get("rate"),
                "rebate_rate": fs.get("rebateRate")}
        infos.append(((start - 30) * 1000, info, start, end))
        for t in (up, dn):
            if t in tok_id:
                by_id[tok_id[t]] = (slug, t)

    from backend.traders.store import update_resolutions
    update_resolutions({s: {"winner": o.lower(), "closed": True} for s, *_, o in wins if o in ("Up", "Down")})

    t0, t1 = con.execute("SELECT MIN(recv_ts), MAX(recv_ts) FROM prices").fetchone()
    h = fix(t0) // 3_600_000 * 3_600_000
    end_all = fix(t1) if a.hours is None else fix(t0) + a.hours * 3_600_000
    books = {}                                   # token id -> {"bids": {}, "asks": {}}
    n_tot, t_wall = 0, time.time()
    while h < end_all:
        h1 = h + 3_600_000
        lo, hi = h - MARGIN_MS, h1 + MARGIN_MS
        ev = []                                  # (t, seq, tag, raw)

        def add(t, tag, raw):
            ev.append((t, len(ev), tag, raw))

        def mine(recv):                          # each row belongs to exactly one hour (by corrected time)
            t = fix(recv)
            return t if h <= t < h1 else None

        for source, tag in (("coinbase", "cb"), ("kraken", "kr"), ("chainlink_spot", "cl")):
            for recv, src, px, rid in con.execute("SELECT recv_ts, src_ts, price, rowid FROM prices WHERE source = ? "
                                                  "AND recv_ts >= ? AND recv_ts < ? ORDER BY recv_ts", (source, lo, hi)):
                t = mine(recv)
                if t is None:
                    continue
                t = timer(tag, tag, t, src)
                p = float(px)
                if tag == "cb":
                    raw = {"type": "ticker", "product_id": "BTC-USD", "price": px, "best_bid": px, "best_ask": px,
                           "time": iso(src or recv)}
                elif tag == "kr":
                    raw = {"channel": "ticker", "type": "update",
                           "data": [{"symbol": "BTC/USD", "last": p, "volume": rid, "timestamp": iso(src or recv)}]}
                else:
                    raw = {"topic": "crypto_prices_chainlink", "type": "update",
                           "payload": {"symbol": "btc/usd", "timestamp": src, "value": p}}
                add(t, tag, json.dumps(raw, separators=(",", ":")))
        fut = "binance_fut_trade" if a.fut == "trade" else "binance_fut_bbo"
        for source, tag in (("binance_spot_bbo", "bn"), (fut, "bf")):
            for recv, src, bid, ask, px in con.execute("SELECT recv_ts, src_ts, bid, ask, price FROM ext_ticks WHERE "
                                                       "source = ? AND recv_ts >= ? AND recv_ts < ? ORDER BY recv_ts",
                                                       (source, lo, hi)):
                t = mine(recv)
                if t is None:
                    continue
                raw = ({"b": bid, "a": ask} if tag == "bn" else
                       {"p": px, "T": src} if a.fut == "trade" else {"b": bid, "a": ask, "T": src})
                add(timer(tag, tag, t, src), tag, json.dumps(raw, separators=(",", ":")))
        for t_info, info, start, end in infos:
            if h <= t_info < h1:
                add(t_info, "info", json.dumps(info))

        # books + trades of the tokens whose window is subscribed in this hour (30 s before .. 90 s after)
        ids = [i for i, (slug, _) in by_id.items()
               if (int(slug.rsplit("-", 1)[1]) - 30) * 1000 < hi and (int(slug.rsplit("-", 1)[1]) + 390) * 1000 > lo]
        for i in sorted(ids):
            slug, tok = by_id[i]
            rows = [(r, s, 0, (b, a)) for r, s, b, a in con.execute(
                "SELECT recv_ts, src_ts, bids, asks FROM book_snap WHERE token = ? AND recv_ts >= ? AND recv_ts < ?",
                (i, lo, hi))]
            rows += [(r, s, 1, (bb, bs, ba, as_)) for r, s, bb, bs, ba, as_ in con.execute(
                "SELECT recv_ts, src_ts, best_bid, bid_size, best_ask, ask_size FROM book_top WHERE token = ? AND "
                "recv_ts >= ? AND recv_ts < ?", (i, lo, hi))]
            rows += [(r, s, 2, (p, sz, side)) for r, s, p, sz, side in con.execute(
                "SELECT recv_ts, src_ts, price, size, side FROM trades WHERE token = ? AND recv_ts >= ? AND recv_ts < ?",
                (i, lo, hi))]
            rows.sort(key=lambda x: (x[0], x[2]))
            tag = f"bk:{slug}"
            for recv, src, kind, d in rows:
                t = mine(recv)
                if t is None:
                    continue                     # the margin rows belong to the neighbour hour
                t = timer(("bk", i), "bk", t, src)
                ts = str(src or recv)
                if kind == 2:
                    add(t, tag, json.dumps({"event_type": "last_trade_price", "asset_id": tok, "price": d[0],
                                            "size": d[1], "side": d[2], "timestamp": ts}, separators=(",", ":")))
                    continue
                cur = books.get(i)
                if kind == 0:
                    new = {"bids": lv(json.loads(d[0] or "[]")), "asks": lv(json.loads(d[1] or "[]"))}
                else:
                    bb, bs, ba, as_ = d
                    old = cur or {"bids": {}, "asks": {}}
                    nb = {} if bb is None else {p: s for p, s in old["bids"].items() if p < float(bb) - 1e-9}
                    na = {} if ba is None else {p: s for p, s in old["asks"].items() if p > float(ba) + 1e-9}
                    if bb is not None:
                        nb[round(float(bb), 4)] = float(bs or 0)
                    if ba is not None:
                        na[round(float(ba), 4)] = float(as_ or 0)
                    new = {"bids": nb, "asks": na}
                new["bids"] = dict(sorted(new["bids"].items(), reverse=True)[:LEVELS])
                new["asks"] = dict(sorted(new["asks"].items())[:LEVELS])
                books[i] = new
                if cur is None:
                    raw = [{"event_type": "book", "asset_id": tok, "timestamp": ts,
                            "bids": [{"price": str(p), "size": str(s)} for p, s in new["bids"].items()],
                            "asks": [{"price": str(p), "size": str(s)} for p, s in new["asks"].items()]}]
                else:
                    ch = []
                    for key, side in (("bids", "BUY"), ("asks", "SELL")):
                        o, nw = cur[key], new[key]
                        for p in set(o) | set(nw):
                            if o.get(p) != nw.get(p):
                                ch.append({"asset_id": tok, "price": str(p), "size": str(nw.get(p, 0)), "side": side})
                    if not ch:
                        continue
                    raw = {"event_type": "price_change", "timestamp": ts, "price_changes": ch}
                add(t, tag, json.dumps(raw, separators=(",", ":")))
        # windows whose subscription ended: forget their books
        for i in [i for i in books if (int(by_id[i][0].rsplit("-", 1)[1]) + 400) * 1000 < h]:
            books.pop(i)

        ev.sort(key=lambda e: (e[0], e[1]))
        if ev:
            path = out_dir / time.strftime("%Y-%m-%d/%H.tsv.gz", time.gmtime(h / 1000))
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wt", encoding="utf-8", compresslevel=4) as f:
                for t, _, tag, raw in ev:
                    f.write(f"{t:.1f}\t{tag}\t{raw}\n")
        n_tot += len(ev)
        print(f"{time.strftime('%Y-%m-%d %H:00', time.gmtime(h / 1000))}  {len(ev):9,} messages  "
              f"({n_tot:,} total, {time.time() - t_wall:.0f} s)", flush=True)
        h = h1
    print(f"done: {out_dir}")


if __name__ == "__main__":
    main()

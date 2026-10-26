"""The recorded book around each real fast-value-live order: what a FAK at the live limit (or the limit + an
allowance) would have bought had it reached the matching engine d ms after it was sent.

Input: reports/fast_allowance/live_orders.json (live_orders rows of the bot; their `ts` is stamped when the response
is back, so the order left at ts - ms). Output: probe.json with, per order and
delay, the ask ladder up to limit + 0.20, plus the window's official winner.

    .venv\\Scripts\\python scripts\\fast_live_probe.py --dir data/recordings_server
"""
import argparse
import json
import sys
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402

DELAYS = tuple(range(-200, 3001, 50))
OUT = Path("reports/fast_allowance")


def probe_slug(args):
    slug, orders, rec_dir = args
    start = int(slug.rsplit("-", 1)[1])
    snaps = [(o["ts"] * 1000 + d, i, d) for i, o in enumerate(orders) for d in DELAYS]
    snaps.sort()
    k = 0
    tokens = None
    books = {"up": {"bids": {}, "asks": {}}, "down": {"bids": {}, "asks": {}}}
    trades = []                                                    # (ms, side, px, size, taker side)
    msgs = []                                                      # recv ms of this market's book messages
    res = [{"book": {}, "trades": []} for _ in orders]
    have_book = False
    end = max(o["ts"] for o in orders) + max(DELAYS) / 1000 + 1

    def take(i, d):
        o = orders[i]
        asks = books[o["side"]]["asks"]
        lim = o["limit"] + 0.20
        res[i]["book"][d] = sorted((p, s) for p, s in asks.items() if p <= lim + 1e-9) if have_book else None
        res[i].setdefault("top", {})[d] = [max(books[s]["bids"], default=None) if k == "bids" else
                                           min(books[s]["asks"], default=None)
                                           for s in ("up", "down") for k in ("bids", "asks")]

    for ms, src, raw in replay.iter_recording(start - 90, end, rec_dir):
        while k < len(snaps) and snaps[k][0] <= ms:
            take(snaps[k][1], snaps[k][2])
            k += 1
        if src == "info" and tokens is None and f'"{slug}"' in raw:
            tokens = {v: s for s, v in json.loads(raw)["tokens"].items()}
            continue
        if src != f"bk:{slug}":
            continue
        msgs.append(ms)
        try:
            d = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        for e in d if isinstance(d, list) else [d]:
            et = e.get("event_type")
            if tokens is None:
                continue
            if et == "book":
                s = tokens.get(e.get("asset_id"))
                if s:
                    books[s]["bids"] = {float(x["price"]): float(x["size"]) for x in e.get("bids", [])}
                    books[s]["asks"] = {float(x["price"]): float(x["size"]) for x in e.get("asks", [])}
                    have_book = True
            elif et == "price_change":
                for c in e.get("price_changes", []):
                    s = tokens.get(c.get("asset_id"))
                    if not s:
                        continue
                    bk = books[s]["bids" if c.get("side") == "BUY" else "asks"]
                    px, sz = float(c["price"]), float(c["size"])
                    if sz <= 0:
                        bk.pop(px, None)
                    else:
                        bk[px] = sz
            elif et == "last_trade_price":
                s = tokens.get(e.get("asset_id"))
                if s:
                    trades.append((ms, s, float(e["price"]), float(e["size"]), e.get("side")))
    while k < len(snaps):
        take(snaps[k][1], snaps[k][2])
        k += 1
    for i, o in enumerate(orders):
        t0 = o["ts"] * 1000
        res[i]["trades"] = [t for t in trades if t0 - 2000 <= t[0] <= t0 + 3000 and t[1] == o["side"]]
        res[i]["tokens_found"] = tokens is not None
        res[i]["msgs"] = [round(x - t0) for x in msgs if t0 - 1000 <= x <= t0 + 4000]
    return slug, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    orders = json.loads((OUT / "live_orders.json").read_text())
    first, last = replay.recording_span(a.dir)
    by = defaultdict(list)
    for o in orders:
        o["ts_resp"], o["ts"] = o["ts"], o["ts"] - (o["ms"] or 0) / 1000
        if first <= o["ts"] < last - 10:
            by[o["slug"]].append(o)
    print(f"{sum(len(v) for v in by.values())} orders in {len(by)} windows", flush=True)
    wins = replay.winners_for(set(by))
    out = []
    with Pool(11) as pool:
        for slug, res in pool.imap_unordered(probe_slug, [(s, v, a.dir) for s, v in by.items()]):
            for o, r in zip(by[slug], res):
                out.append({**o, **r, "winner": wins.get(slug)})
    out.sort(key=lambda o: o["ts"])
    (OUT / "probe.json").write_text(json.dumps(out))
    print(f"wrote {len(out)} orders", flush=True)


if __name__ == "__main__":
    main()

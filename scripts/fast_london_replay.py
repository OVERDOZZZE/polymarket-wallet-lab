"""Fast player (value): does London's timing change the taker fill rate? Paper prediction for the fast-london live test.

    .venv\\Scripts\\python -m scripts.fast_london_replay --from "2026-10-25 00:00" --to "2026-10-26 00:00"

fast-value-live's config on the Dublin recordings (data/recordings_server), either as recorded ("dublin") or re-timed
to London's feeds (latency_profile p50 London - Dublin: Kraken -20 ms, Coinbase -7, Binance spot +2, futures +21,
Polymarket book -8, Chainlink -27). Fill model: the queue model (queue_max_ms 2200, which reproduced Dublin's live
~10% fill rate) and the 50 ms paper model for reference. Per variant: orders, filled, fill %, net PnL. Places no orders.
"""
import argparse
import collections
import heapq
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.config import DATA_DIR  # noqa: E402

SHIFT_MS = {"kr": -20.4, "cb": -7.0, "bn": 1.6, "bf": 21.0, "bk": -8.0, "cl": -26.7}
_ORIG_ITER = replay.iter_recording
FV = {"bankroll": 500.0, "windows": [5], "sources": ["coinbase", "kraken", "binance"], "spike_bps": 2.0,
      "lookback_ms": 1000.0, "combine": "mean", "feed_stale_ms": 3000.0, "fair_mode": "anchored", "min_edge": 0.02,
      "extra_pay": 0.0, "min_price": 0.05, "max_price": 0.6, "start_delay_s": 2.0, "stop_before_end_s": 3.0,
      "max_entries_per_window": 2, "cooldown_s": 5.0, "stake_frac": 0.04, "max_stake_usd": 100.0, "exit_mode": "hold",
      "max_window_frac": 0.2, "max_open_frac": 0.5, "max_daily_loss_frac": 0.0, "spot_source": "coinbase",
      "vol_floor": 0.4, "latency_ms": 50.0, "match_wait_ms": 1500.0, "fee_rate": 0.07, "rebate_share": 0.15}
Q = {"queue_max_ms": 2200.0}
VARIANTS = [("fv-queue-dublin", "dublin", Q), ("fv-queue-london", "london", Q),
            ("fv-paper-dublin", "dublin", {}), ("fv-paper-london", "london", {})]
VARIANTS2 = [("fv-q-london-pay2", "london", {**Q, "extra_pay": 0.02}),     # --set 2: can any variant pay on the queue model?
             ("fv-q-london-pay3", "london", {**Q, "extra_pay": 0.03}),
             ("fv-q-london-bf", "london", {**Q, "sources": ["coinbase", "kraken", "binance", "binance_fut"]}),
             ("fv-q-london-bf-pay2", "london", {**Q, "sources": ["coinbase", "kraken", "binance", "binance_fut"], "extra_pay": 0.02}),
             ("fv-q-london-cap70", "london", {**Q, "max_price": 0.70})]


def retimed(start_ts, end_ts, rec_dir=None):
    h, seq = [], 0
    for ms, src, raw in _ORIG_ITER(start_ts, end_ts, rec_dir):
        t = ms + SHIFT_MS.get("bk" if src.startswith("bk:") else src, 0.0)
        heapq.heappush(h, (t, seq, src, raw))
        seq += 1
        while h and h[0][0] < ms - 30.0:
            t, _, s, r = heapq.heappop(h)
            yield t, s, r
    while h:
        t, _, s, r = heapq.heappop(h)
        yield t, s, r


def _chunk(args):
    vs, timing, s, e, rec_dir = args
    replay.iter_recording = retimed if timing == "london" else _ORIG_ITER
    res = replay.run([(n, "fast_player", ov, FV) for n, _t, ov in vs], s, e, rec_dir=rec_dir)
    out = {}
    for k, r in res.items():
        c = r["counts"]
        out[k] = {"orders": c.get("orders", 0), "filled": c.get("buy_filled", 0) + c.get("buy_partial", 0),
                  "missed": sum(v for x, v in c.items() if x.startswith("buy_") and x not in ("buy_filled", "buy_partial")),
                  "net": sum(w["net_pnl"] for w in r["windows"]), "windows": len(r["windows"]),
                  "cost": sum(w["cost"] for w in r["windows"]), "counts": dict(c)}
    return s, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--dir", default="data/recordings_server")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--slice-h", type=float, default=2.0)
    ap.add_argument("--set", type=int, default=1, help="1: London vs Dublin, 2: variants (re-timed), 3: variants on "
                                                       "recordings made in London (--dir data/recordings, no shift)")
    a = ap.parse_args()
    global VARIANTS
    if a.set == 2:
        VARIANTS = VARIANTS2
    elif a.set == 3:                                    # "dublin" = as recorded: these recordings are London's own
        VARIANTS = [(n.replace("london", "native"), "dublin", ov) for n, _t, ov in [VARIANTS[1]] + VARIANTS2[:2] + VARIANTS2[3:4]]
    s, e = replay._ts(a.start), replay._ts(a.end)
    step, lead = a.slice_h * 3600, 120
    chunks = []
    for timing in ("london", "dublin"):
        g = [v for v in VARIANTS if v[1] == timing]
        chunks += [(g, timing, t - lead, min(t + step, e), a.dir) for t in range(int(s), int(e), int(step))]
    print(f"{len(VARIANTS)} variants, {len(chunks)} processes", flush=True)
    tot = collections.defaultdict(lambda: collections.Counter())
    t0 = time.time()
    from concurrent.futures import ProcessPoolExecutor, as_completed
    with ProcessPoolExecutor(max_workers=min(a.workers, len(chunks))) as ex:
        for i, fu in enumerate(as_completed([ex.submit(_chunk, c) for c in chunks]), 1):
            _, res = fu.result()
            for k, r in res.items():
                for x in ("orders", "filled", "missed", "net", "windows", "cost"):
                    tot[k][x] += r[x]
                tot[k]["_counts"] = 0
                for x, v in r["counts"].items():
                    if x.startswith(("buy_", "sig_", "live_")) or x in ("orders", "spikes"):
                        tot[k]["c:" + x] += v
            print(f"  {i}/{len(chunks)} ({time.time() - t0:.0f} s)", flush=True)
    print(f"\n{'variant':18s} {'orders':>7s} {'filled':>7s} {'fill%':>6s} {'net $':>9s} {'cost $':>9s} {'ROI%':>6s}")
    for n, *_ in VARIANTS:
        r = tot[n]
        print(f"{n:18s} {r['orders']:7d} {r['filled']:7d} {100 * r['filled'] / max(1, r['orders']):6.1f} {r['net']:9.2f} "
              f"{r['cost']:9.2f} {100 * r['net'] / max(1e-9, r['cost']):6.1f}")
    d = DATA_DIR / "replays"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"fast_london-{a.start[:10]}-set{a.set}.json"
    f.write_text(json.dumps({k: dict(v) for k, v in tot.items()}, indent=1))
    print("saved", f)


if __name__ == "__main__":
    main()

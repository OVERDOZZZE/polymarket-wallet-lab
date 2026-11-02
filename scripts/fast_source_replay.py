"""Fast player (value): which BTC feed makes the best spike signal? fast-london's config with each feed alone and in
combinations. Places no orders.

    .venv\\Scripts\\python -m scripts.fast_source_replay --dir data/recordings_london --from "2026-11-01 08:20" --to "2026-11-02 02:00"
    .venv\\Scripts\\python -m scripts.fast_source_replay --dir data/recordings_server --from "2026-10-24 00:00" --to "2026-10-28 00:00" --retime

--retime: Dublin recordings shifted to London's feed timing (scripts/fast_london_replay.SHIFT_MS). Fill models: the queue
model (queue_max_ms 2200, which reproduced live fill rates) and the 50 ms paper model (fills almost every order: only a
measure of the signal itself, not of real fills).
"""
import argparse
import collections
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.config import DATA_DIR  # noqa: E402
from scripts import fast_london_replay as fl  # noqa: E402

CB, KR, BN, BF = "coinbase", "kraken", "binance", "binance_fut"
SIGNALS = [("base", [CB, KR, BN], "mean"), ("cb", [CB], "mean"), ("kr", [KR], "mean"), ("bn", [BN], "mean"),
           ("bf", [BF], "mean"), ("bn+bf", [BN, BF], "mean"), ("mean4", [CB, KR, BN, BF], "mean"),
           ("max3", [CB, KR, BN], "max"), ("max4", [CB, KR, BN, BF], "max")]


def variants(timing):
    out = []
    for n, src, comb in SIGNALS:
        out.append((f"q-{n}", timing, {**fl.Q, "sources": src, "combine": comb}))
        out.append((f"p-{n}", timing, {"sources": src, "combine": comb}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--retime", action="store_true")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--slice-h", type=float, default=2.0)
    a = ap.parse_args()
    timing = "london" if a.retime else "dublin"          # fast_london_replay: "dublin" = as recorded
    vs = variants(timing)
    s, e = replay._ts(a.start), replay._ts(a.end)
    step, lead = a.slice_h * 3600, 120
    chunks = [(vs, timing, t - lead, min(t + step, e), a.dir) for t in range(int(s), int(e), int(step))]
    print(f"{len(vs)} variants, {len(chunks)} processes", flush=True)
    tot = collections.defaultdict(collections.Counter)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=min(a.workers, len(chunks))) as ex:
        for i, fu in enumerate(as_completed([ex.submit(fl._chunk, c) for c in chunks]), 1):
            _, res = fu.result()
            for k, r in res.items():
                for x in ("orders", "filled", "net", "windows", "cost"):
                    tot[k][x] += r[x]
                tot[k]["spikes"] += r["counts"].get("spikes", 0)
            print(f"  {i}/{len(chunks)} ({time.time() - t0:.0f} s)", flush=True)
    print(f"\n{a.dir} {a.start} -> {a.end} {'(re-timed to London)' if a.retime else '(as recorded)'}")
    print(f"{'signal':8s} | {'queue model (realistic)':^40s} | {'50 ms paper (signal only)':^32s}")
    print(f"{'':8s} | {'orders':>7s} {'filled':>6s} {'fill%':>6s} {'net $':>8s} {'ROI%':>6s} | "
          f"{'orders':>7s} {'filled':>6s} {'net $':>9s} {'ROI%':>6s}")
    for n, *_ in SIGNALS:
        q, p = tot[f"q-{n}"], tot[f"p-{n}"]
        print(f"{n:8s} | {q['orders']:7d} {q['filled']:6d} {100 * q['filled'] / max(1, q['orders']):6.1f} "
              f"{q['net']:8.2f} {100 * q['net'] / max(1e-9, q['cost']):6.1f} | {p['orders']:7d} {p['filled']:6d} "
              f"{p['net']:9.2f} {100 * p['net'] / max(1e-9, p['cost']):6.1f}")
    d = DATA_DIR / "replays"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"fast_source-{Path(a.dir).name}-{a.start[:10]}.json"
    f.write_text(json.dumps({k: dict(v) for k, v in tot.items()}, indent=1))
    print("saved", f)


if __name__ == "__main__":
    main()

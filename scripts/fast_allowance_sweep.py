"""Sweep fast_player settings on recorded data with live-like order delays (reports/fast_allowance/README.md).

Every variant starts from fast-value-live's config with a fixed $ order size (no compounding, so variants compare
per order) and the measured live FAK round trips as `latency_samples`. The span is cut into blocks; each
(block x variant chunk) runs as its own process through the replay engine and caches its result, so a crash or a
restart only redoes that job. Windows count in the block they start in.

    .venv\\Scripts\\python scripts\\fast_allowance_sweep.py --dir data/recordings_server --from "2026-10-24 00:00" \\
        --to "2026-10-26 05:00" --grid allowance --name allowance
"""
import argparse
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "reports" / "fast_allowance"

LIVE_MS = [o["ms"] for o in json.loads((OUT / "live_orders.json").read_text()) if o.get("ms")]
BASE = {"windows": [5], "sources": ["coinbase", "kraken", "binance"], "spike_bps": 2.0, "lookback_ms": 1000.0,
        "combine": "mean", "feed_stale_ms": 3000.0, "fair_mode": "anchored", "min_edge": 0.02, "extra_pay": 0.0,
        "min_price": 0.05, "max_price": 0.6, "start_delay_s": 2.0, "stop_before_end_s": 3.0,
        "max_entries_per_window": 2, "cooldown_s": 5.0, "exit_mode": "hold", "spot_source": "coinbase",
        "vol_floor": 0.4, "latency_ms": 50.0, "latency_samples": LIVE_MS, "match_wait_ms": 1500.0, "fee_rate": 0.07,
        # fixed $15 orders (live size), limits that never bind
        "bankroll": 1e6, "stake_frac": 0.04, "max_stake_usd": 15.0, "max_daily_loss_frac": 1.0}


def grid(name):
    if name == "allowance":
        out = [{"latency_samples": [], "extra_pay": 0.0}]                      # paper as it runs today
        for mp, ep in itertools.product((0.60, 0.70), (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10)):
            out.append({"max_price": mp, "extra_pay": ep})
        return out
    if name == "queue":                                                        # queue fill model (calibrated)
        out = []
        for mp, ep in itertools.product((0.60, 0.70, 0.85), (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)):
            out.append({"queue_max_ms": 2200, "max_price": mp, "extra_pay": ep})
        for ep in (0.0, 0.02):
            out.append({"queue_max_ms": 1500, "max_price": 0.6, "extra_pay": ep})
        return out
    if name == "test":
        return [{"extra_pay": 0.0}, {"extra_pay": 0.02}]
    p = OUT / f"grid_{name}.json"                                              # written by hand for later stages
    if p.exists():
        return json.loads(p.read_text())
    raise SystemExit(f"unknown grid {name}")


# ------------------------------------------------------------------ one job (subprocess)
def run_job(spec):
    from backend.bots import replay
    from backend.bots.strategies.fast_player import FastBot

    class Logged(FastBot):
        def _close(self, o, result):
            if o in self.pending and o.buy:
                self.order_log.append({"ts": round(o.placed / 1000.0, 3), "slug": o.m.slug, "side": o.side, "result": result, "limit": o.limit,
                                       "ask": o.sig.get("ask"), "fair": o.sig.get("fair"), "lat": round(o.lat),
                                       "filled": o.sig.get("filled"), "avg": o.sig.get("avg"),
                                       "t_left": o.sig.get("t_left"), "move": o.sig.get("move")})
            super()._close(o, result)

    def make(name, cfg):
        b = Logged(name, cfg)
        b.order_log = []
        return b

    score0 = replay.score
    replay.score = lambda b: {**score0(b), "orders": b.order_log}
    s, e = spec["start"], spec["end"]
    variants = [(name, "fast_player", ov, BASE, make) for name, ov in spec["variants"]]
    res = replay.run(variants, s - 90, e + 420, rec_dir=spec["dir"], warmup_s=90)
    out = {}
    for name, r in res.items():
        start_of = lambda slug: int(slug.rsplit("-", 1)[1])
        out[name] = {"windows": [{k: w[k] for k in ("slug", "start", "cost", "trading_pnl", "winner", "up_sh", "down_sh")}
                                 for w in r["windows"] if s <= w["start"] < e],
                     "orders": [o for o in r["orders"] if s <= start_of(o["slug"]) < e]}
    Path(spec["out"]).write_text(json.dumps(out))


# ------------------------------------------------------------------ driver
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/recordings_server")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--grid", default="allowance")
    ap.add_argument("--name")
    ap.add_argument("--block-h", type=float, default=6)
    ap.add_argument("--per-proc", type=int, default=3)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--job", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.job:
        run_job(json.loads(Path(a.job).read_text()))
        return
    from backend.bots.replay import _ts
    s0, e0 = _ts(a.start), _ts(a.end)
    jobdir = OUT / "jobs" / a.name
    jobdir.mkdir(parents=True, exist_ok=True)
    variants = [(json.dumps(ov, sort_keys=True), ov) for ov in grid(a.grid)]
    chunks = [variants[i:i + a.per_proc] for i in range(0, len(variants), a.per_proc)]
    blocks, t = [], s0
    while t < e0:
        blocks.append((t, min(e0, t + a.block_h * 3600)))
        t += a.block_h * 3600
    todo = []
    for (bs, be), (ci, ch) in itertools.product(blocks, enumerate(chunks)):
        key = f"{int(bs)}_{ci}"
        spec = {"start": bs, "end": be, "dir": a.dir, "variants": ch, "out": str(jobdir / f"{key}.json")}
        sp = jobdir / f"{key}.spec.json"
        sp.write_text(json.dumps(spec))
        if not Path(spec["out"]).exists():
            todo.append((key, sp))
    print(f"{len(variants)} variants x {len(blocks)} blocks: {len(todo)} jobs to run", flush=True)
    running, tries, t0 = {}, {}, time.time()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    while todo or running:
        while todo and len(running) < a.workers:
            key, sp = todo.pop(0)
            log = open(jobdir / f"{key}.log", "w")
            running[key] = (subprocess.Popen([sys.executable, __file__, "--job", str(sp)], cwd=ROOT, stdout=log,
                                             stderr=subprocess.STDOUT, env=env), sp, log)
        time.sleep(2)
        for key, (p, sp, log) in list(running.items()):
            if p.poll() is None:
                continue
            log.close()
            del running[key]
            ok = p.returncode == 0 and (jobdir / f"{key}.json").exists()
            tries[key] = tries.get(key, 0) + 1
            print(f"{time.strftime('%H:%M:%S')} job {key} {'ok' if ok else f'FAILED rc={p.returncode}'} "
                  f"({(time.time() - t0) / 60:.0f} min, {len(todo)} queued, {len(running)} running)", flush=True)
            if not ok and tries[key] < 3:
                todo.append((key, sp))
    merge(a.name, variants, blocks)


def merge(name, variants, blocks):
    jobdir = OUT / "jobs" / name
    res = {n: {"ov": ov, "windows": [], "orders": [], "blocks": 0} for n, ov in variants}
    for f in jobdir.glob("*_*.json"):
        if f.name.endswith(".spec.json"):
            continue
        for n, r in json.loads(f.read_text()).items():
            if n in res:
                res[n]["windows"] += r["windows"]
                res[n]["orders"] += r["orders"]
                res[n]["blocks"] += 1
    (OUT / f"{name}.json").write_text(json.dumps(res))
    print(f"merged -> {OUT / f'{name}.json'}")


if __name__ == "__main__":
    main()

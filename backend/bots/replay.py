"""Offline replay: recorded market data (backend/recorder.py) -> any number of bot configurations, in one pass.

Every variant is a real engine instance (PaperBot or a subclass) built offline (`engine(name, cfg)`: no DB,
no sockets). Recorded messages are parsed once and handed to each engine's handlers with the clock set to the
message's receive time; the engines' 50 ms ticker runs on the same virtual clock. So quoting, the queue-aware
fill simulation, latency, takers and settlement are exactly the live paper code. Windows are scored on
Polymarket's official winner (data/cache/resolutions.json, fetched from Gamma when missing).

    python -m backend.bots.replay --hours 6 --variant crouch_v2 --variant market_maker:base_edge=0.02
    python -m backend.bots.replay --from "2026-10-22 07:00" --to "2026-10-22 12:00" --bot crouch-v2
    python -m backend.bots.replay --dir data/import/btc5m/recordings_dublin --hours 64 --variant fast_player
      (--dir: another recordings folder, e.g. the old logger PC's data converted by scripts/import_btc5m.py)

From code: `run([("name", "crouch_v2", {...overrides}), ...], start_ts, end_ts)` -> {name: result}.
"""
import argparse
import collections
import json
import re
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import orjson

from . import registry
from ..config import RECORDINGS_DIR
from ..polymarket.markets import fetch_winner
from ..traders.store import load_resolutions, update_resolutions

TICK_MS = 50


class _Null:
    def put(self, *_):
        pass

    def qsize(self):
        return 0


def recording_span(rec_dir=None):
    """(first, last) receive time in seconds over all recordings, or (None, None)."""
    files = sorted(Path(rec_dir or RECORDINGS_DIR).glob("*/*.tsv.gz"))
    if not files:
        return None, None

    def hour(p):
        return datetime.strptime(f"{p.parent.name} {p.stem.split('.')[0]}", "%Y-%m-%d %H").replace(
            tzinfo=timezone.utc).timestamp()
    return hour(files[0]), hour(files[-1]) + 3600


def iter_recording(start_ts, end_ts, rec_dir=None):
    """(recv_ms, source, raw) in recorded order for start_ts <= recv < end_ts (see _gz_lines for broken files)."""
    t = int(start_ts) // 3600 * 3600
    while t < end_ts:
        path = Path(rec_dir or RECORDINGS_DIR) / time.strftime("%Y-%m-%d/%H.tsv.gz", time.gmtime(t))
        t += 3600
        if not path.exists():
            continue
        for line in _gz_lines(path):
            try:
                ms, src, raw = line.split("\t", 2)
                ms = float(ms)
            except ValueError:
                continue
            if ms < start_ts * 1000:
                continue
            if ms >= end_ts * 1000:
                return
            yield ms, src, raw


# a member header as Python's gzip writes it: flags 0 or FNAME, mtime, xfl, OS 255 (FNAME: "HH.tsv\0" follows)
_GZ_HEADER = re.compile(rb"\x1f\x8b\x08([\x00\x08])(....)[\x00\x02\x04]\xff", re.S)
_GZ_NAME = re.compile(rb"[\w.\-]{1,64}\x00")


def _member_starts(data):
    out = []
    for m in _GZ_HEADER.finditer(data):
        if not 1.5e9 < int.from_bytes(m.group(2), "little") < 2.5e9:
            continue
        if m.group(1) == b"\x08" and not _GZ_NAME.match(data, m.end()):
            continue
        out.append(m.start())
    return out or [0]


def _gz_lines(path, chunk=1 << 16):
    """Lines of a multi-member gzip file. The recorder opens one member per start; a killed recorder leaves its
    member unterminated, and zlib then silently swallows the next member (gzip.open stops there: the rest of the
    hour was lost). So the file is split at the member headers (magic + a plausible mtime) and each member is
    decoded on its own, as far as it goes. A file still being written ends in a partial member: read up to there."""
    data = path.read_bytes()
    starts = _member_starts(data)
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(data)
        d, buf = zlib.decompressobj(31), b""
        for i in range(s, e, chunk):
            try:
                buf += d.decompress(data[i:min(i + chunk, e)])
            except zlib.error:
                break
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                yield ln.decode("utf-8", "replace")
            if d.eof:
                break
        if d.eof and buf:
            yield buf.decode("utf-8", "replace")


def winners_for(slugs):
    res = {k: (v or {}).get("winner") for k, v in load_resolutions().items()}
    new = {}
    for s in slugs:
        if not res.get(s):
            try:
                w = fetch_winner(s)
            except Exception:
                w = None
            if w:
                new[s] = w
    if new:
        update_resolutions({k: {"winner": v, "closed": True} for k, v in new.items()})
        res.update(new)
    return {s: res.get(s) for s in slugs}


def build(name, strategy_name, overrides=None, base=None, engine=None):
    """An offline engine. `engine` overrides the strategy's class (research probes)."""
    eng, cfg_cls = registry.strategy(strategy_name)
    cfg = {**cfg_cls().to_dict(), **(base or {}), **(overrides or {})}
    b = (engine or eng)(name, cfg)
    b._wq = _Null()
    b.fills = collections.deque()
    b.markouts = collections.deque()
    b._t = 0.0
    b.clock = lambda b=b: b._t
    return b


def run(variants, start_ts, end_ts, progress=None, warmup_s=90, rec_dir=None):
    """variants: [(name, strategy, overrides[, base_config[, engine class]])]. Windows that start before start_ts + warmup_s are
    not traded (the engines need the previous minute of Chainlink ticks for the strike)."""
    from .strategies.market_maker import Market
    bots = [build(*v) for v in variants]
    trade_from = start_ts + warmup_s
    next_tick, last_prog = None, 0.0
    t_wall = time.time()
    n = 0
    bf_bots = [b for b in bots if hasattr(b, "_on_binance_fut_msg")]
    kr_bots = [b for b in bots if hasattr(b, "_on_kraken_msg")]
    for ms, src, raw in iter_recording(start_ts, end_ts, rec_dir):
        n += 1
        if next_tick is None:
            next_tick = (ms // TICK_MS + 1) * TICK_MS
        while next_tick <= ms:
            for b in bots:
                b._t = next_tick / 1000.0
                b._tick(next_tick)
            next_tick += TICK_MS
        for b in bots:
            b._t = ms / 1000.0
        if src.startswith("bk:"):
            slug = src[3:]
            try:
                d = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            for b in bots:
                m = b.markets.get(slug)
                if m is not None:
                    b._on_book_msg(m, d)
        elif src == "cb":
            d = orjson.loads(raw)
            for b in bots:
                b._on_coinbase_msg(d)
        elif src == "bn":
            d = orjson.loads(raw)
            for b in bots:
                b._on_binance_msg(d)
        elif src == "kr":
            if kr_bots:
                d = orjson.loads(raw)
                for b in kr_bots:
                    b._on_kraken_msg(d)
        elif src == "bf":
            if bf_bots:
                d = orjson.loads(raw)
                for b in bf_bots:
                    b._on_binance_fut_msg(d)
        elif src == "cl":
            try:
                d = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            for b in bots:
                b._on_chainlink_msg(d)
        elif src == "info":
            info = json.loads(raw)
            slug = info["slug"]
            mins = int(slug.split("-")[2][:-1])
            start = int(slug.rsplit("-", 1)[1])
            for b in bots:
                if mins in b.cfg.windows and slug not in b.markets and start >= trade_from:
                    b.markets[slug] = Market(slug, mins, start, info)
        elif src == "act":
            low = raw.lower()
            targets = [b for b in bots if getattr(b.cfg, "target_wallet", None) and b.cfg.target_wallet in low]
            if targets:
                d = orjson.loads(raw)
                for b in targets:
                    b._on_target_msg(d)
        if progress and time.time() - last_prog > 5:
            last_prog = time.time()
            progress((ms / 1000.0 - start_ts) / max(1.0, end_ts - start_ts), n, time.time() - t_wall)
    # let the last windows close and settle on the clock
    if next_tick is not None:
        stop = next_tick + 40_000
        while next_tick <= stop:
            for b in bots:
                b._t = next_tick / 1000.0
                b._tick(next_tick)
            next_tick += TICK_MS
    return {b.bot_id: score(b) for b in bots}


def _run_chunk(args):
    variants, start_ts, end_ts, warmup_s, rec_dir = args
    return run(variants, start_ts, end_ts, warmup_s=warmup_s, rec_dir=rec_dir)


def run_parallel(variants, start_ts, end_ts, workers=6, per_proc=3, warmup_s=90, rec_dir=None):
    """run() with the variants split over processes (each reads the recording itself)."""
    from concurrent.futures import ProcessPoolExecutor
    chunks = [variants[i:i + per_proc] for i in range(0, len(variants), per_proc)]
    out = {}
    with ProcessPoolExecutor(max_workers=min(workers, len(chunks))) as ex:
        for r in ex.map(_run_chunk, [(c, start_ts, end_ts, warmup_s, rec_dir) for c in chunks]):
            out.update(r)
    return out


def score(b):
    """Window records re-scored on Polymarket's official winners + the fills, as plain dicts."""
    recs = [r for r in b.settled if r["start"] + r["win"] * 60 <= b._t]
    wins = winners_for({r["slug"] for r in recs})
    out = []
    for r in recs:
        w = wins.get(r["slug"]) or r.get("winner")
        if not w:
            continue
        pay = r["up_sh"] if w == "up" else r["down_sh"]
        out.append({**r, "winner": w, "trading_pnl": pay - r["cost"], "net_pnl": pay - r["cost"] + r["rebate_est"]})
    fills = [dict(f) for f in b.fills]
    for f in fills:
        w = wins.get(f["slug"])
        f["won"] = None if w is None else int(f["side"] == w)
    return {"windows": out, "fills": fills, "counts": dict(b.counts), "cfg": b.cfg.to_dict()}


def summary(res):
    w = res["windows"]
    cost = sum(r["cost"] for r in w)
    tr = sum(r["trading_pnl"] for r in w)
    rb = sum(r["rebate_est"] for r in w)
    return {"windows": len(w), "cost": cost, "trading": tr, "rebate": rb, "net": tr + rb,
            "roi": (tr + rb) / cost if cost else 0.0, "won": sum(1 for r in w if r["net_pnl"] > 0),
            "fills": len(res["fills"]), "taker_fills": sum(1 for f in res["fills"] if not f["maker"])}


def _parse_variant(text):
    """strategy[:key=value,key=value]  (values are JSON, e.g. windows=[5])"""
    strat, _, rest = text.partition(":")
    ov = {}
    for kv in filter(None, rest.split(",")) if rest and "[" not in rest else ([rest] if rest else []):
        k, _, v = kv.partition("=")
        ov[k.strip()] = json.loads(v)
    return strat, ov


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="start", help="UTC 'YYYY-MM-DD HH:MM' (default: --hours before the end)")
    ap.add_argument("--to", dest="end", help="UTC 'YYYY-MM-DD HH:MM' (default: end of the recordings)")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--variant", action="append", default=[], help="strategy[:key=json,...]")
    ap.add_argument("--bot", action="append", default=[], help="an existing bot's current config")
    ap.add_argument("--dir", default=None, help="recordings folder (default: data/recordings)")
    a = ap.parse_args()
    first, last = recording_span(a.dir)
    if first is None:
        raise SystemExit("no recordings yet: run python -m backend.recorder")
    end = _ts(a.end) if a.end else min(last, time.time())
    start = _ts(a.start) if a.start else max(first, end - a.hours * 3600)
    variants = []
    for bid in a.bot:
        row = registry.get_bot(bid)
        variants.append((bid, row["strategy"], {}, row["config"]))
    for i, v in enumerate(a.variant):
        s, ov = _parse_variant(v)
        variants.append((f"v{i}:{v}", s, ov))
    res = run(variants, start, end, rec_dir=a.dir, progress=lambda p, n, el: print(f"  {p:5.1%}  {n:,} msgs  {el:.0f}s", flush=True))
    for name, r in res.items():
        s = summary(r)
        print(f"{name:40s} windows {s['windows']:4d}  cost ${s['cost']:9,.0f}  trading ${s['trading']:+8,.2f}  "
              f"rebate ${s['rebate']:7,.2f}  net ${s['net']:+8,.2f}  roi {s['roi']:+.2%}  fills {s['fills']}")


if __name__ == "__main__":
    main()

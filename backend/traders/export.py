"""Zip exports of everything stored for one or more traders."""
import io
import json
import time
import zipfile

from . import store
from .. import db
from ..polymarket.markets import ASSETS, market_map

README = """Polymarket Wallet Lab export - {name}
wallet: {wallet}
market: {asset} {window}-minute Up/Down windows
coverage (UTC): {coverage}
exported: {now} UTC

trader.json             settings, covered date ranges, notes
activity.parquet        raw activity rows (all markets) for the covered ranges
analysis/report.md      strategy report
analysis/summary.json   all headline metrics
analysis/windows.csv    one row per traded window (cost, payout, pnl, pair cost, ...)
analysis/buys.parquet   every resolved buy with timing, outcome and model edge
analysis/*.csv          tables behind the charts (timing, price, hourly, daily, edge deciles)
live_events.jsonl       live-captured events (trades with receive time + latency, redeems, ...)
resolutions.json        winners of every market in the activity
klines/                 (optional) 1s reference prices for the traded days
last_job.log            log of the last download / analysis job
"""


def _add(z, t, prefix, include_prices):
    z.writestr(prefix + "trader.json", json.dumps(t.to_dict(), indent=2))
    for f in [t.activity_path, t.log_path, *sorted(t.analysis_dir.glob("*"))]:
        if f.exists() and f.is_file():
            z.write(f, prefix + str(f.relative_to(t.dir)).replace("\\", "/"))
    ev = db.q("SELECT data FROM trader_events WHERE trader_id = ? ORDER BY recv_ms", (t.id,))
    if ev:
        z.writestr(prefix + "live_events.jsonl", "\n".join(r["data"] for r in ev) + "\n")
    cov = ", ".join(f"{time.strftime('%Y-%m-%d', time.gmtime(s))} to {time.strftime('%Y-%m-%d', time.gmtime(e))}"
                    for s, e in t.coverage) or "none"
    z.writestr(prefix + "README.txt", README.format(
        name=t.name, wallet=t.wallet, asset=t.asset, window=t.window_minutes, coverage=cov,
        now=time.strftime("%Y-%m-%d %H:%M", time.gmtime())))
    act = store.load_activity(t, columns=["slug", "title", "timestamp"])
    if len(act):
        res = store.load_resolutions()
        slugs = set(act["slug"].dropna().astype(str))
        z.writestr(prefix + "resolutions.json", json.dumps({s: v for s, v in res.items() if s in slugs}))
        if include_prices:
            sym = ASSETS[t.asset]["symbol"]
            days = set()
            for ws, wl in market_map(act, t.asset, t.window_minutes).values():
                days.update({time.strftime("%Y-%m-%d", time.gmtime(x)) for x in (ws - 1000, ws + wl)})
            for d in sorted(days):
                f = store.kline_path(sym, d)
                if f.exists():
                    z.write(f, f"{prefix}klines/{sym}/{d}.csv")


def build_zip(traders, include_prices=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        multi = len(traders) > 1
        for t in traders:
            _add(z, t, f"{t.id}/" if multi else "", include_prices)
    return buf.getvalue()


def size_estimate_mb(t, include_prices=False):
    total = sum(f.stat().st_size for f in t.dir.rglob("*") if f.is_file()) if t.dir.exists() else 0
    if include_prices:
        total += 1.6e6 * sum((e - s) / 86400 + 1 for s, e in t.coverage)
    return total / 1e6

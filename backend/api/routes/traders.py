"""Tracked traders: CRUD, download/analysis jobs, analysis outputs, live watching, exports."""
import os
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from ..state import hub, jobs, records
from ...polymarket.markets import ASSETS, WINDOW_CHOICES, window_bounds, window_slug
from ...traders import export, pipeline, store
from ...traders.sources import FetchError, parse_identity, resolve_handle
from ...util import read_json, read_parquet_where

router = APIRouter(prefix="/api/traders", tags=["traders"])
TABLES = {"timing", "price", "hourly", "daily", "edge_deciles"}
WINDOW_COLS = ["win_start", "slug", "winner", "pnl", "cost", "n_buys", "up_sh", "up_cost", "down_sh", "down_cost",
               "avg_up", "avg_down", "pair_cost", "locked_profit", "two_sided", "redeem_usd"]


def _trader(tid):
    try:
        return store.Trader.load(tid)
    except FileNotFoundError:
        raise HTTPException(404, f"trader '{tid}' not found")


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


@lru_cache(maxsize=48)
def _csv(path, mtime):
    return pd.read_csv(path, low_memory=False)


def read_csv(path):
    m = _mtime(path)
    return None if m is None else _csv(str(path), m)


def summary(t):
    return read_json(t.analysis_dir / "summary.json", None)


def card(t):
    S = summary(t) or {}
    j = jobs.running_for(t.id)
    sc = S.get("signals") or {}
    return {**t.to_dict(), "short_wallet": store.short_wallet(t.wallet), "watching": hub.is_watching(t.id),
            "job": j.to_dict() if j else None,
            "status": ("downloading" if j else "error" if t.last_error else
                       "ready" if S and not S.get("empty") else "no trades" if S else "no data"),
            "metrics": {k: S.get(k) for k in ("windows", "total_pnl", "deployed", "roi", "win_rate_windows", "sharpe",
                                              "max_drawdown", "two_sided_share", "pair_cost_median")}
            | {"favourite": sc.get("aligned"), "edge_corr": sc.get("edge_spearman")}}


@router.get("")
def list_traders():
    return {"traders": [card(t) for t in store.list_traders()], "assets": list(ASSETS), "windows": WINDOW_CHOICES}


class NewTrader(BaseModel):
    ident: str
    name: str = ""
    asset: str = "BTC"
    window_minutes: int = 5
    start: date
    end: date
    live: bool = False


@router.post("")
def create_trader(body: NewTrader):
    try:
        pipeline.day_bounds(body.start, body.end)
        kind, val = parse_identity(body.ident)
        handle = ""
        if kind == "handle":
            handle = val
            val, _ = resolve_handle(val)
        dup = store.find_by_wallet(val, body.asset, body.window_minutes)
        if dup:
            raise HTTPException(409, f'This wallet is already tracked as "{dup.name}" ({body.asset}, '
                                     f'{body.window_minutes} min). Open it and use Update data to extend the period.')
        t = store.create(body.name.strip() or handle or store.short_wallet(val), val, body.asset,
                         body.window_minutes, handle)
    except (ValueError, FetchError) as e:
        raise HTTPException(400, str(e))
    if body.live:
        t = store.patch(t.id, live_autostart=True)
        hub.watch(t.wallet, t.id, t.asset, t.window_minutes)
    jobs.start(t, "download", start=body.start, end=body.end)
    return card(t)


@router.get("/{tid}")
def get_trader(tid: str):
    t = _trader(tid)
    j = jobs.latest_for(tid)
    return {**card(t), "summary": summary(t), "last_job": j.to_dict() if j else None}


class TraderPatch(BaseModel):
    name: str | None = None
    asset: str | None = None
    window_minutes: int | None = None
    live_autostart: bool | None = None
    notes: str | None = None


@router.patch("/{tid}")
def patch_trader(tid: str, body: TraderPatch):
    t = _trader(tid)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "name" in fields and not fields["name"].strip():
        raise HTTPException(400, "The name can't be empty.")
    if jobs.running_for(tid) and ({"asset", "window_minutes"} & set(fields)):
        raise HTTPException(409, "A job is running for this trader; change the market once it finishes.")
    changed = (fields.get("asset", t.asset), fields.get("window_minutes", t.window_minutes)) != (t.asset, t.window_minutes)
    t = store.patch(tid, **fields)
    if t.live_autostart or hub.is_watching(tid):
        hub.watch(t.wallet, tid, t.asset, t.window_minutes)
    if changed:
        jobs.start(t, "analysis", fetch=False)
    return card(t)


@router.delete("/{tid}")
def delete_trader(tid: str):
    if jobs.running_for(tid):
        raise HTTPException(409, "Cancel the running job first.")
    hub.unwatch(tid)
    store.delete(tid)
    return {"ok": True}


# ---------------------------------------------------------------- jobs
def _range(start, end, t):
    today = datetime.now(timezone.utc).date()
    if start is None:
        start = datetime.fromtimestamp(t.coverage[0][0], timezone.utc).date() if t.coverage else today - timedelta(days=7)
    return start, end or today


@router.get("/{tid}/download-preview")
def download_preview(tid: str, start: date | None = None, end: date | None = None):
    t = _trader(tid)
    start, end = _range(start, end, t)
    try:
        s, e = pipeline.day_bounds(start, end)
    except ValueError as err:
        return {"gaps": [], "message": str(err), "start": start, "end": end}
    gaps = store.missing_intervals(s, e, t.coverage)
    if not gaps:
        return {"gaps": [], "message": "Everything in this period is already stored. Re-analyse instead.",
                "start": start, "end": end}
    hrs = sum(b - a for a, b in gaps) / 3600
    parts = ", ".join(f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(a))} to "
                      f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(b))}" for a, b in gaps[:3])
    more = f" (+{len(gaps) - 3} more)" if len(gaps) > 3 else ""
    return {"gaps": gaps, "message": f"Will download {hrs:,.1f} hours: {parts}{more} UTC.", "start": start, "end": end}


class Period(BaseModel):
    start: date | None = None
    end: date | None = None


@router.post("/{tid}/update")
def update_data(tid: str, body: Period):
    t = _trader(tid)
    start, end = _range(body.start, body.end, t)
    try:
        return jobs.start(t, "download", start=start, end=end).to_dict()
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.post("/{tid}/analyse")
def analyse(tid: str):
    try:
        return jobs.start(_trader(tid), "analysis", fetch=False).to_dict()
    except RuntimeError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------- analysis outputs
@router.get("/{tid}/tables")
def tables(tid: str):
    t = _trader(tid)
    return {name: records(read_csv(t.analysis_dir / f"{name}.csv")) for name in TABLES}


@router.get("/{tid}/windows")
def windows(tid: str):
    t = _trader(tid)
    w = read_csv(t.analysis_dir / "windows.csv")
    if w is None:
        return {"rows": []}
    return {"rows": records(w[[c for c in WINDOW_COLS if c in w.columns]].sort_values("win_start", ascending=False))}


@router.get("/{tid}/pnl")
def pnl_series(tid: str, points: int = 1500):
    """Cumulative PnL per resolved window (thinned), plus cumulative cost for return-on-deployed."""
    t = _trader(tid)
    w = read_csv(t.analysis_dir / "windows.csv")
    if w is None or "pnl" not in w:
        return {"t": [], "cum": [], "cost": []}
    d = w.dropna(subset=["pnl"]).sort_values("win_start")
    cum, cost = d.pnl.cumsum(), d.cost.cumsum()
    idx = list(range(0, len(d), max(1, len(d) // points)))
    if len(d) and idx[-1] != len(d) - 1:
        idx.append(len(d) - 1)
    return {"t": d.win_start.iloc[idx].astype(int).tolist(), "cum": cum.iloc[idx].round(2).tolist(),
            "cost": cost.iloc[idx].round(2).tolist(),
            "hist": d.pnl.round(2).tolist()[-20000:]}


@router.get("/{tid}/window/{slug}")
def window_detail(tid: str, slug: str):
    """Every buy in one window + the 1s reference price path around it."""
    t = _trader(tid)
    path = t.analysis_dir / "buys.parquet"
    buys = pd.DataFrame()
    if path.exists():
        buys = read_parquet_where(path, "slug", slug)
    w = read_csv(t.analysis_dir / "windows.csv")
    row = None
    if w is not None:
        r = w[w.slug == slug]
        row = records(r)[0] if len(r) else None
    ws = int(slug.rsplit("-", 1)[-1]) if slug.rsplit("-", 1)[-1].isdigit() else int((row or {}).get("win_start") or 0)
    L = t.window_minutes * 60
    sym = ASSETS[t.asset]["symbol"]
    parts = []
    for d in {time.strftime("%Y-%m-%d", time.gmtime(ws - 60)), time.strftime("%Y-%m-%d", time.gmtime(ws + L + 60))}:
        k = read_csv(store.kline_path(sym, d))
        if k is not None:
            parts.append(k)
    k = pd.concat(parts) if parts else pd.DataFrame(columns=["t", "close"])
    k = k[(k.t >= ws - 60) & (k.t <= ws + L + 30)].sort_values("t")
    strike = None
    if len(k):
        strike = float(k.loc[k.t <= ws, "close"].iloc[-1]) if (k.t <= ws).any() else float(k.close.iloc[0])
    return {"window": row, "start": ws, "length": L, "symbol": sym, "strike": strike,
            "prices": {"t": (k.t - ws).tolist(), "v": k.close.tolist()}, "buys": records(buys)}


@router.get("/{tid}/report", response_class=PlainTextResponse)
def report(tid: str):
    p = _trader(tid).analysis_dir / "report.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@router.get("/{tid}/files")
def files(tid: str):
    t = _trader(tid)
    out = []
    if t.dir.exists():
        for f in sorted(t.dir.rglob("*")):
            if f.is_file():
                st = f.stat()
                out.append({"file": str(f.relative_to(t.dir)).replace("\\", "/"), "mb": round(st.st_size / 1e6, 2),
                            "modified": st.st_mtime})
    n = len(hub.get(tid).events) if hub.get(tid) else 0
    log = t.log_path.read_text(encoding="utf-8")[-15000:] if t.log_path.exists() else ""
    return {"dir": str(t.dir), "files": out, "log": log, "live_events_in_memory": n}


@router.get("/{tid}/export")
def export_one(tid: str, prices: bool = False):
    t = _trader(tid)
    data = export.build_zip([t], prices)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{t.id}_{time.strftime("%Y%m%d-%H%M")}.zip"'})


@router.get("/{tid}/export-size")
def export_size(tid: str, prices: bool = False):
    return {"mb": export.size_estimate_mb(_trader(tid), prices)}


@router.post("/{tid}/open-folder")
def open_folder(tid: str):
    t = _trader(tid)
    t.dir.mkdir(parents=True, exist_ok=True)
    os.startfile(t.dir)
    return {"ok": True}


# ---------------------------------------------------------------- live watching
@router.post("/{tid}/watch")
def watch(tid: str):
    t = _trader(tid)
    hub.watch(t.wallet, t.id, t.asset, t.window_minutes)
    return {"ok": True}


@router.delete("/{tid}/watch")
def unwatch(tid: str):
    hub.unwatch(tid)
    return {"ok": True}


def _ladder(book, depth=10):
    if not book:
        return None
    return {"bids": book["bids"][:depth], "asks": book["asks"][:depth], "last": book.get("last")}


@router.get("/{tid}/live")
def live(tid: str):
    t = _trader(tid)
    w = hub.get(tid)
    lat = hub.latency_summary()
    base = {"watching": w is not None, "hub": {"rtds": hub.status["rtds"], "clob": hub.status["clob"],
                                               "rtds_error": hub.status.get("rtds_error"),
                                               "clob_error": hub.status.get("clob_error"),
                                               "msgs": hub.stats["rtds_msgs"], "last_ms": hub.stats["rtds_last_ms"],
                                               "clock_offset_ms": hub.clock_offset_ms, **lat}}
    if w is None:
        return base
    start, end = window_bounds(t.window_minutes)
    slug = window_slug(t.asset, t.window_minutes, start)
    mk = hub.market(slug) or {}
    toks = mk.get("tokens", {})
    strike, ssrc = hub.strike(t.asset, start)
    tl, pb = sorted(w.trade_lag), sorted(w.poll_behind)
    series = hub.price_series(t.asset, (start - 60) * 1000)
    mids = {}
    for o in ("up", "down"):
        tok = toks.get(o)
        ms = [m for m in list(hub.mids.get(tok, ())) if m[0] >= start * 1000] if tok else []
        mids[o] = [((m[0] / 1000) - start, (m[1] + m[2]) / 2) for m in ms if m[1] is not None and m[2] is not None]
    return {**base,
            "watch": {"since": w.since, "n_ws": w.n_ws, "n_poll": w.n_poll, "value": w.value, "poll_error": w.poll_error,
                      "trade_lag_p50": tl[len(tl) // 2] if tl else None, "trade_lag_n": len(tl),
                      "poll_behind_p50": pb[len(pb) // 2] if pb else None},
            "window": {"slug": slug, "start": start, "end": end, "title": mk.get("title") or slug,
                       "strike": strike, "strike_src": ssrc, "last": hub.last_price(t.asset)},
            "books": {"up": _ladder(hub.book(toks.get("up"))), "down": _ladder(hub.book(toks.get("down")))},
            "prices": [((ts / 1000) - start, v) for ts, v in series[-2000:]], "mids": mids,
            "events": list(w.events)[-300:], "positions": w.positions[:100]}


# ---------------------------------------------------------------- many-trader export
export_router = APIRouter(prefix="/api", tags=["traders"])


@export_router.get("/export")
def export_many(ids: str, prices: bool = False):
    ts = [_trader(x) for x in ids.split(",") if x]
    data = export.build_zip(ts, prices)
    name = ts[0].id if len(ts) == 1 else "wallet-lab"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}_{time.strftime("%Y%m%d-%H%M")}.zip"'})

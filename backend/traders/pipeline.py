"""Fetch -> resolve -> reference prices -> analyse, for one trader.

`job` is any object with .log(msg), .progress(frac, stage), .check_cancel() and .cancel_event
(backend/jobs.py runs this in a child process; ConsoleJob is for the CLI)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import pandas as pd

from . import analysis, store
from ..polymarket.markets import ASSETS, market_map
from ..util import write_df_atomic
from .sources import Cancelled, FetchError, fetch_activity, fetch_klines_1s, fetch_resolution


class ConsoleJob:
    def __init__(self):
        self.cancel_event = threading.Event()

    def log(self, msg):
        print(msg)

    def progress(self, frac, stage=""):
        print(f"  [{frac * 100:5.1f}%] {stage}", end="\r")

    def check_cancel(self):
        pass


def day_bounds(start, end):
    """date/str -> (start_ts at 00:00:00 UTC, end_ts at 23:59:59 UTC, clamped to now)."""
    s = pd.Timestamp(start).date() if not isinstance(start, date) else start
    e = pd.Timestamp(end).date() if not isinstance(end, date) else end
    if e < s:
        raise ValueError("End date is before start date.")
    st = int(datetime(s.year, s.month, s.day, tzinfo=timezone.utc).timestamp())
    en = int(datetime(e.year, e.month, e.day, tzinfo=timezone.utc).timestamp()) + 86399
    now = int(time.time())
    if st > now:
        raise ValueError("Start date is in the future.")
    return st, min(en, now)


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def step_activity(t, start_ts, end_ts, job, lo=0.0, hi=0.45):
    gaps = store.missing_intervals(start_ts, end_ts, t.coverage)
    if not gaps:
        job.log("activity: requested range already stored, nothing to download")
        return
    total = sum(e - s for s, e in gaps) or 1
    done = 0
    for s, e in gaps:
        job.log(f"activity: fetching {_fmt(s)} -> {_fmt(e)} UTC")
        base = done

        def prog(f, msg, s=s, e=e, base=base):
            job.progress(lo + (hi - lo) * (base + f * (e - s)) / total, f"Activity: {msg}")

        try:
            rows = fetch_activity(t.wallet, s, e, log=job.log, progress=prog, cancel=job.cancel_event)
        except Cancelled as c:
            if c.partial and c.reached_ts is not None and c.reached_ts < e:
                _store_activity(t, c.partial, c.reached_ts + 1, e)
                job.log(f"activity: kept {len(c.partial):,} rows for {_fmt(c.reached_ts + 1)} -> {_fmt(e)}")
            raise
        _store_activity(t, rows, s, e)
        done += e - s
        job.log(f"activity: +{len(rows):,} rows")


def _store_activity(t, rows, s, e):
    """Merge rows into activity.parquet, then mark [s, e] as covered (only after the rows are on disk)."""
    df = store.merge_activity(store.load_activity(t), rows, s, e)
    store.save_activity(t, df)
    cur = store.Trader.load(t.id)
    fields = {"coverage": store.merge_intervals(cur.coverage + [[s, e]])}
    if rows and not cur.pm_name:
        fields["pm_name"] = str(rows[0].get("name") or "")
        fields["pm_pseudonym"] = str(rows[0].get("pseudonym") or "")
        if cur.name == store.short_wallet(cur.wallet) and fields["pm_name"]:
            fields["name"] = fields["pm_name"]
    store.patch(t.id, **fields)


def step_resolutions(t, act, job, lo=0.45, hi=0.65):
    markets = market_map(act, t.asset, t.window_minutes)
    cache = store.load_resolutions()
    now = time.time()
    cond = act.drop_duplicates("slug").set_index("slug")["conditionId"].to_dict()
    todo = [s for s, (ws, wl) in markets.items()
            if not (cache.get(s) or {}).get("winner") and ws + wl < now - 600 and isinstance(cond.get(s), str)]
    job.log(f"markets: {len(markets):,} {t.asset} {t.window_minutes}m windows traded; "
            f"{len(todo):,} resolutions to fetch ({len(markets) - len(todo):,} cached)")
    if not todo:
        job.progress(hi, "Resolutions cached")
        return
    new, errs, first_err = {}, 0, None
    with ThreadPoolExecutor(6) as ex:
        futs = {ex.submit(fetch_resolution, cond[s], job.cancel_event): s for s in todo}
        try:
            for i, f in enumerate(as_completed(futs)):
                s = futs[f]
                try:
                    new[s] = f.result()
                except Cancelled:
                    raise
                except Exception as e:
                    errs += 1
                    first_err = first_err or f"{s}: {e}"
                if i % 25 == 0:
                    job.progress(lo + (hi - lo) * i / len(todo), f"Resolutions {i:,}/{len(todo):,}")
                    job.check_cancel()
                if len(new) >= 500:
                    store.update_resolutions(new)
                    new = {}
        except Cancelled:
            for f in futs:
                f.cancel()
            store.update_resolutions(new)
            raise
    store.update_resolutions(new)
    job.log(f"resolutions: done, {errs} errors" + (f" (first: {first_err})" if first_err else ""))


def needed_days(act, t):
    days = set()
    for ws, wl in market_map(act, t.asset, t.window_minutes).values():
        for x in (ws - 1000, ws + wl):          # 900 s look-back for the volatility estimate
            days.add(time.strftime("%Y-%m-%d", time.gmtime(x)))
    return days


def step_klines(t, act, job, lo=0.65, hi=0.9):
    symbol = ASSETS[t.asset]["symbol"]
    days = needed_days(act, t)
    markets = market_map(act, t.asset, t.window_minutes)
    need_until = max((ws + wl for ws, wl in markets.values()), default=None)
    todo = sorted(d for d in days if not store.kline_day_complete(symbol, d, need_until))
    job.log(f"{symbol} 1s prices: {len(days)} days needed, {len(todo)} to download")
    if not todo:
        return days

    def one(day):
        d0 = int(pd.Timestamp(day, tz="UTC").timestamp())
        d1 = min(d0 + 86400, int(time.time()))
        rows = fetch_klines_1s(symbol, d0, d1, cancel=job.cancel_event)
        if rows:
            write_df_atomic(pd.DataFrame(rows, columns=["t", "close"]), store.kline_path(symbol, day))
        return day, len(rows)

    failed = []
    with ThreadPoolExecutor(3) as ex:
        futs = {ex.submit(one, d): d for d in todo}
        for i, f in enumerate(as_completed(futs)):
            try:
                f.result()
            except Cancelled:
                for g in futs:
                    g.cancel()
                raise
            except Exception as e:
                failed.append(futs[f])
                job.log(f"warning: prices for {futs[f]} failed: {e}")
            job.progress(lo + (hi - lo) * (i + 1) / len(todo), f"{symbol} prices {i + 1}/{len(todo)} days")
            job.check_cancel()
    if failed:
        job.log(f"warning: {len(failed)} price days missing; signal tests will skip those trades")
    return days


def step_analysis(t, act, days, job):
    job.progress(0.92, "Analysing")
    job.log("analysis: running")
    res = store.load_resolutions()
    kl = store.load_klines(ASSETS[t.asset]["symbol"], days) if days else None
    A = analysis.run(act, res, kl, t.asset, t.window_minutes, label=t.name)
    analysis.save(A, t.analysis_dir)
    S = A.summary
    if S.get("empty"):
        job.log(f"analysis: no {t.asset} {t.window_minutes}m trades in the stored data")
    else:
        job.log(f"analysis: {S['windows']:,} windows, pnl ${S.get('total_pnl') or 0:,.0f}")
    return A


def run_update(tid, start=None, end=None, job=None, fetch=True):
    """Download what's missing for [start, end] (UTC dates), then refresh resolutions, prices and the
    analysis. fetch=False just re-analyses the stored data."""
    job = job or ConsoleJob()
    t = store.Trader.load(tid)
    try:
        if fetch:
            start_ts, end_ts = day_bounds(start, end)
            step_activity(t, start_ts, end_ts, job)
        job.check_cancel()
        act = store.load_activity(t)
        if act.empty:
            job.log("no activity stored for this wallet in the covered range")
        days = set()
        if not act.empty:
            try:
                step_resolutions(t, act, job)
            except Cancelled:
                raise
            except Exception as e:
                job.log(f"warning: resolution step failed ({e}); continuing with cached results")
            job.check_cancel()
            try:
                days = step_klines(t, act, job)
            except Cancelled:
                raise
            except Exception as e:
                job.log(f"warning: price step failed ({e}); continuing without signal tests")
                days = needed_days(act, t)
        job.check_cancel()
        step_analysis(store.Trader.load(tid), act, days, job)
        store.patch(tid, updated=time.time(), last_error="")
        job.progress(1.0, "Done")
    except Cancelled:
        raise
    except Exception as e:
        try:
            store.patch(tid, last_error=f"{type(e).__name__}: {e}")
        except Exception:
            pass
        raise


__all__ = ["run_update", "day_bounds", "ConsoleJob", "FetchError", "Cancelled"]

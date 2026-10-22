"""Tracked traders (wallets): DB rows + per-trader files + the shared caches.

DB  traders                               settings, coverage, notes   (backend/db.py)
DB  trader_events                         live-captured actions        (backend/traders/live.py)
data/traders/<id>/activity.parquet        raw activity rows (all markets) for the covered date ranges
data/traders/<id>/analysis/               outputs of the last analysis run (summary.json, *.csv, buys.parquet)
data/traders/<id>/last_job.log            log of the last download / analysis job
data/cache/resolutions.json               market slug -> {"winner", "closed"}   (shared)
data/cache/klines/<SYMBOL>/<day>.csv      Binance 1s closes per UTC day         (shared)
"""
import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field

import pandas as pd

from .. import db
from ..config import CACHE_DIR, TRADERS_DIR
from ..util import read_json, write_df_atomic, write_text_atomic

RES_PATH = CACHE_DIR / "resolutions.json"
ACTIVITY_COLS = ["timestamp", "type", "side", "outcome", "outcomeIndex", "price", "size", "usdcSize",
                 "slug", "title", "eventSlug", "conditionId", "asset", "transactionHash"]
_lock = threading.RLock()
_res_lock = threading.Lock()


# ---------------------------------------------------------------- coverage intervals [[start, end], ...] inclusive
def merge_intervals(iv):
    out = []
    for s, e in sorted((int(a), int(b)) for a, b in iv if b >= a):
        if out and s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def missing_intervals(start, end, coverage):
    gaps, cur = [], start
    for s, e in merge_intervals(coverage):
        if e < cur:
            continue
        if s > end:
            break
        if s > cur:
            gaps.append([cur, s - 1])
        cur = max(cur, e + 1)
    if cur <= end:
        gaps.append([cur, end])
    return gaps


# ---------------------------------------------------------------- trader
@dataclass
class Trader:
    id: str
    name: str
    wallet: str
    asset: str = "BTC"
    window_minutes: int = 5
    handle: str = ""
    pm_name: str = ""
    pm_pseudonym: str = ""
    coverage: list = field(default_factory=list)
    notes: str = ""
    created: float = field(default_factory=time.time)
    updated: float = 0.0
    last_error: str = ""
    live_autostart: bool = False

    @property
    def dir(self):
        return TRADERS_DIR / self.id

    @property
    def activity_path(self):
        return self.dir / "activity.parquet"

    @property
    def analysis_dir(self):
        return self.dir / "analysis"

    @property
    def log_path(self):
        return self.dir / "last_job.log"

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_row(cls, r):
        r = dict(r)
        r["coverage"] = db.loads(r.get("coverage"), [])
        r["live_autostart"] = bool(r.get("live_autostart"))
        return cls(**{k: v for k, v in r.items() if k in cls.__dataclass_fields__})

    def save(self):
        d = asdict(self)
        d["coverage"] = db.dumps(d["coverage"])
        d["live_autostart"] = int(bool(d["live_autostart"]))
        cols = ", ".join(d)
        db.x(f"INSERT OR REPLACE INTO traders ({cols}) VALUES ({', '.join('?' * len(d))})", tuple(d.values()))

    @classmethod
    def load(cls, tid):
        r = db.one("SELECT * FROM traders WHERE id = ?", (tid,))
        if not r:
            raise FileNotFoundError(f"trader '{tid}' not found")
        return cls.from_row(r)


def patch(tid, **fields):
    """Load-modify-save so UI edits and background jobs never overwrite each other's fields."""
    with _lock:
        t = Trader.load(tid)
        for k, v in fields.items():
            if k not in Trader.__dataclass_fields__ or k == "id":
                raise KeyError(k)
            setattr(t, k, v)
        t.save()
        return t


def short_wallet(w):
    return f"{w[:6]}…{w[-4:]}" if w else ""


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return s[:40] or "trader"


def list_traders():
    return [Trader.from_row(r) for r in db.q("SELECT * FROM traders ORDER BY created")]


def find_by_wallet(wallet, asset=None, window_minutes=None):
    for t in list_traders():
        if t.wallet.lower() == wallet.lower() and (asset is None or t.asset == asset) \
                and (window_minutes is None or t.window_minutes == window_minutes):
            return t
    return None


def create(name, wallet, asset="BTC", window_minutes=5, handle=""):
    with _lock:
        base = slugify(name)
        tid, i = base, 2
        while db.one("SELECT id FROM traders WHERE id = ?", (tid,)) or (TRADERS_DIR / tid).exists():
            tid, i = f"{base}-{i}", i + 1
        t = Trader(id=tid, name=name.strip() or tid, wallet=wallet.lower(), asset=asset,
                   window_minutes=int(window_minutes), handle=handle)
        t.dir.mkdir(parents=True, exist_ok=True)
        t.save()
        return t


def delete(tid):
    """Remove the trader, its live events and its folder (shared caches are kept)."""
    db.x("DELETE FROM traders WHERE id = ?", (tid,))
    db.x("DELETE FROM trader_events WHERE trader_id = ?", (tid,))
    d = TRADERS_DIR / tid
    for i in range(5):
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return
        time.sleep(0.4 * (i + 1))


# ---------------------------------------------------------------- activity
def load_activity(t, columns=None):
    if not t.activity_path.exists():
        return pd.DataFrame(columns=columns or ACTIVITY_COLS)
    return _plain_text(pd.read_parquet(t.activity_path, columns=columns))


def _plain_text(df):
    """Text columns as object dtype with NaN for missing (the analysis expects NaN, not pd.NA)."""
    for c in df.columns:
        if isinstance(df[c].dtype, pd.StringDtype):
            df[c] = df[c].astype(object).where(df[c].notna(), float("nan"))
    return df


def merge_activity(old, new_rows, start_ts, end_ts):
    """Replace everything stored for [start_ts, end_ts] with new_rows (a complete download of that range).
    No row-level de-duplication: identical fills in one transaction are real, so ranges are swapped wholesale."""
    new = pd.DataFrame(new_rows)
    if len(new):
        new = new[[c for c in ACTIVITY_COLS if c in new.columns]]
    if len(old):
        ts = pd.to_numeric(old["timestamp"], errors="coerce")
        old = old[(ts < start_ts) | (ts > end_ts)]
    parts = [x for x in (old, new) if len(x)]
    if not parts:
        return pd.DataFrame(columns=ACTIVITY_COLS)
    df = pd.concat(parts, ignore_index=True)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("int64")
    return normalise_activity(df.sort_values("timestamp", ascending=False, kind="stable").reset_index(drop=True))


def normalise_activity(df):
    """Stable dtypes so parquet files always share one schema."""
    for c in ("price", "size", "usdcSize"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "outcomeIndex" in df:
        df["outcomeIndex"] = pd.to_numeric(df["outcomeIndex"], errors="coerce")
    for c in ("type", "side", "outcome", "slug", "title", "eventSlug", "conditionId", "asset", "transactionHash"):
        if c in df:   # str where present, None where missing: one parquet schema, plain objects in pandas
            df[c] = df[c].astype(object).where(df[c].notna(), None).map(lambda v: v if v is None else str(v))
    return df


def save_activity(t, df):
    write_df_atomic(df, t.activity_path)


# ---------------------------------------------------------------- shared caches
def load_resolutions():
    return read_json(RES_PATH, {})


def update_resolutions(new):
    """Merge new results into the shared cache (never overwrite a known winner with None)."""
    with _res_lock:
        cur = load_resolutions()
        for k, v in new.items():
            if v is None:
                continue
            if (cur.get(k) or {}).get("winner") and not v.get("winner"):
                continue
            cur[k] = v
        write_text_atomic(RES_PATH, json.dumps(cur))
        return cur


def kline_path(symbol, day):
    return CACHE_DIR / "klines" / symbol / f"{day}.csv"


def kline_day_complete(symbol, day, need_until=None):
    path = kline_path(symbol, day)
    if not path.exists():
        return False
    try:
        t = pd.read_csv(path, usecols=["t"])["t"]
    except Exception:
        return False
    d0 = int(pd.Timestamp(day, tz="UTC").timestamp())
    end = d0 + 86400 if need_until is None else min(d0 + 86400, int(need_until))
    return len(t) > 0 and t.min() <= d0 + 120 and t.max() >= end - 120


def load_klines(symbol, days):
    parts = []
    for d in sorted(days):
        path = kline_path(symbol, d)
        if path.exists():
            try:
                parts.append(pd.read_csv(path))
            except Exception:
                pass
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True).drop_duplicates("t").sort_values("t").reset_index(drop=True)

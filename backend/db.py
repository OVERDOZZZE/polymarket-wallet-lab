"""The app's own SQLite database (DB_PATH, default data/btc5m.db).

Holds what is ours and must survive: traders we track, what they did live, bots, everything bots did,
and our live orders/trades. Market data is NOT stored here (the data PC records it).

WAL mode: one writer at a time, readers never block. Every process/thread opens its own connection
via conn(). Rows come back as dicts. JSON columns are named `data`/`config`/`coverage`.
"""
import json
import sqlite3
import threading

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, wallet TEXT NOT NULL, asset TEXT DEFAULT 'BTC',
    window_minutes INTEGER DEFAULT 5, handle TEXT DEFAULT '', pm_name TEXT DEFAULT '',
    pm_pseudonym TEXT DEFAULT '', coverage TEXT DEFAULT '[]', notes TEXT DEFAULT '',
    created REAL, updated REAL DEFAULT 0, last_error TEXT DEFAULT '', live_autostart INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trader_events (          -- live-captured actions of tracked wallets
    id INTEGER PRIMARY KEY, trader_id TEXT NOT NULL, recv_ms REAL, ts INTEGER, kind TEXT, source TEXT,
    side TEXT, outcome TEXT, price REAL, size REAL, usdc REAL, slug TEXT, tx TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS ix_trader_events ON trader_events (trader_id, recv_ms);

CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, strategy TEXT NOT NULL, mode TEXT DEFAULT 'paper',
    config TEXT DEFAULT '{}', account INTEGER DEFAULT 1, account_since REAL, bankroll REAL DEFAULT 1000,
    created REAL, notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bot_fills (
    id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, account INTEGER NOT NULL, ts REAL, slug TEXT, win INTEGER,
    side TEXT, price REAL, shares REAL, usd REAL, maker INTEGER, rebate REAL, fee REAL, t_in REAL,
    fair REAL, btc REAL, markout REAL
);
CREATE INDEX IF NOT EXISTS ix_bot_fills ON bot_fills (bot_id, account, ts);
CREATE TABLE IF NOT EXISTS bot_windows (             -- one row per traded window, upserted until official
    bot_id TEXT NOT NULL, account INTEGER NOT NULL, slug TEXT NOT NULL, start INTEGER, win INTEGER,
    net_pnl REAL, data TEXT, PRIMARY KEY (bot_id, account, slug)
);
CREATE TABLE IF NOT EXISTS bot_equity (
    bot_id TEXT NOT NULL, account INTEGER NOT NULL, ts REAL, v REAL, cash REAL
);
CREATE INDEX IF NOT EXISTS ix_bot_equity ON bot_equity (bot_id, account, ts);
CREATE TABLE IF NOT EXISTS bot_changes (             -- per-bot history: what it is, rebuilds, audits, setting changes
    id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, ts REAL, kind TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS ix_bot_changes ON bot_changes (bot_id, ts);

CREATE TABLE IF NOT EXISTS live_orders (             -- orders sent from this app (manual or a live bot)
    order_id TEXT PRIMARY KEY, ts REAL, origin TEXT, slug TEXT, token_id TEXT, outcome TEXT, side TEXT,
    order_type TEXT, price REAL, size REAL, amount REAL, status TEXT, filled REAL DEFAULT 0,
    latency_ms REAL, error TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS ix_live_orders_ts ON live_orders (ts);
CREATE TABLE IF NOT EXISTS live_trades (             -- our fills, from the CLOB user channel
    id TEXT PRIMARY KEY, ts REAL, order_id TEXT, slug TEXT, token_id TEXT, outcome TEXT, side TEXT,
    price REAL, size REAL, status TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS ix_live_trades_ts ON live_trades (ts);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        global _initialised
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        with _init_lock:
            if not _initialised:
                c.executescript(SCHEMA)
                _initialised = True
        _local.c = c
    return c


def q(sql, args=()):
    """SELECT -> list of dicts."""
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, args=()):
    """INSERT/UPDATE/DELETE (autocommit)."""
    return conn().execute(sql, args)


def many(sql, rows):
    c = conn()
    c.execute("BEGIN")
    try:
        c.executemany(sql, rows)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def dumps(v):
    return json.dumps(v, default=str, separators=(",", ":"))


def loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default

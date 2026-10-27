"""Paper ledger in SQLite (logs/spikebot.db): signals, simulated fills, per-window positions and settlement.
Cash is rebuilt from the tables on start, so a restart continues the same $1,000 account."""
import json
import sqlite3
import time

from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL, slug TEXT, tin REAL, move_bps REAL, side TEXT, ask REAL, ask_prev REAL,
  action TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, ts REAL, signal_ts REAL, slug TEXT, tin REAL, side TEXT, token TEXT, move_bps REAL,
  ask_signal REAL, ask_prev REAL, limit_px REAL, shares_req REAL, shares REAL, avg_px REAL, cost REAL, fee REAL,
  levels TEXT);
CREATE TABLE IF NOT EXISTS windows (
  slug TEXT PRIMARY KEY, start INTEGER, up_sh REAL DEFAULT 0, dn_sh REAL DEFAULT 0, cost REAL DEFAULT 0,
  fee REAL DEFAULT 0, winner TEXT, payout REAL, pnl REAL, settled_ts REAL);
CREATE TABLE IF NOT EXISTS equity (ts REAL, cash REAL, open_cost REAL, realized REAL, trades INTEGER);
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, detail TEXT);
"""


class Ledger:
    def __init__(self, path=C.DB_PATH):
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        spent = self.db.execute("SELECT COALESCE(SUM(cost + fee), 0) FROM trades").fetchone()[0]
        paid = self.db.execute("SELECT COALESCE(SUM(payout), 0) FROM windows WHERE winner IS NOT NULL").fetchone()[0]
        self.cash = C.BANKROLL_USD - spent + paid

    def event(self, kind, detail=""):
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (time.time(), kind, str(detail)[:2000]))

    def signal(self, ts, slug, tin, move, side, ask, ask_prev, action, reason=""):
        self.db.execute("INSERT INTO signals (ts, slug, tin, move_bps, side, ask, ask_prev, action, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (ts, slug, tin, move, side, ask, ask_prev, action, reason))

    def fill(self, t):
        """t: dict with the trades columns. Debits cash and adds the shares to the window's position."""
        self.db.execute("INSERT INTO trades (ts, signal_ts, slug, tin, side, token, move_bps, ask_signal, ask_prev, "
                        "limit_px, shares_req, shares, avg_px, cost, fee, levels) VALUES "
                        "(:ts, :signal_ts, :slug, :tin, :side, :token, :move_bps, :ask_signal, :ask_prev, :limit_px, "
                        ":shares_req, :shares, :avg_px, :cost, :fee, :levels)", {**t, "levels": json.dumps(t["levels"])})
        col = "up_sh" if t["side"] == "up" else "dn_sh"
        self.db.execute("INSERT OR IGNORE INTO windows (slug, start) VALUES (?, ?)",
                        (t["slug"], int(t["slug"].rsplit("-", 1)[1])))
        self.db.execute(f"UPDATE windows SET {col} = {col} + ?, cost = cost + ?, fee = fee + ? WHERE slug = ?",
                        (t["shares"], t["cost"], t["fee"], t["slug"]))
        self.cash -= t["cost"] + t["fee"]

    def window_spent(self, slug):
        r = self.db.execute("SELECT cost + fee FROM windows WHERE slug = ?", (slug,)).fetchone()
        return r[0] if r else 0.0

    def unsettled(self):
        return self.db.execute("SELECT slug, start, up_sh, dn_sh, cost, fee FROM windows WHERE winner IS NULL").fetchall()

    def settle(self, slug, winner):
        up_sh, dn_sh, cost, fee = self.db.execute("SELECT up_sh, dn_sh, cost, fee FROM windows WHERE slug = ?",
                                                  (slug,)).fetchone()
        payout = up_sh if winner == "up" else dn_sh
        pnl = payout - cost - fee
        self.db.execute("UPDATE windows SET winner = ?, payout = ?, pnl = ?, settled_ts = ? WHERE slug = ?",
                        (winner, payout, pnl, time.time(), slug))
        self.cash += payout
        return pnl

    def snapshot(self):
        open_cost = self.db.execute("SELECT COALESCE(SUM(cost + fee), 0) FROM windows WHERE winner IS NULL").fetchone()[0]
        realized = self.db.execute("SELECT COALESCE(SUM(pnl), 0) FROM windows WHERE winner IS NOT NULL").fetchone()[0]
        n = self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        return {"cash": self.cash, "open_cost": open_cost, "realized": realized, "trades": n,
                "equity_at_cost": self.cash + open_cost}

    def record_equity(self):
        s = self.snapshot()
        self.db.execute("INSERT INTO equity VALUES (?, ?, ?, ?, ?)",
                        (time.time(), s["cash"], s["open_cost"], s["realized"], s["trades"]))
        return s

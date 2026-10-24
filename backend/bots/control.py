"""Start, stop and talk to bot processes from the API server.

Each bot runs in its own high-priority process (python -m backend.bots.runner <bot_id>) so nothing in the
server (page requests, analysis jobs, the trade gateway) competes with its feed handlers. Per bot:
  data/bots/<id>/state.json    live snapshot, rewritten twice a second by the bot
  data/bots/<id>/control.json  commands to the bot: {"id", "action": stop|reset|reset_day|config, ...}
  data/bots/<id>/bot.pid       process id while running
  data/bots/<id>/bot.log       process output
"""
import json
import time

from . import registry
from .. import db
from ..config import BOTS_DIR, ROOT
from ..util import kill_pid, pid_alive, read_json, spawn, write_text_atomic

FRESH_S = 4.0


def bot_dir(bot_id):
    d = BOTS_DIR / bot_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_state(bot_id):
    return read_json(bot_dir(bot_id) / "state.json", None)


def pid(bot_id):
    try:
        return int((bot_dir(bot_id) / "bot.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def is_running(bot_id, state=None):
    if pid_alive(pid(bot_id)):
        return True
    state = state if state is not None else read_state(bot_id)
    return bool(state and state.get("running") and time.time() - float(state.get("ts") or 0) < FRESH_S)


def start(bot_id):
    registry.get_bot(bot_id)
    if pid_alive(pid(bot_id)):
        return False
    d = bot_dir(bot_id)
    proc = spawn(["-m", "backend.bots.runner", bot_id, "--quiet"], ROOT, d / "bot.log")
    (d / "bot.pid").write_text(str(proc.pid))
    return True


def send(bot_id, action, **kw):
    write_text_atomic(bot_dir(bot_id) / "control.json", json.dumps({"id": time.time_ns(), "action": action, **kw}))


def stop(bot_id, timeout=6.0):
    p = pid(bot_id)
    if not pid_alive(p):
        return
    send(bot_id, "stop")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not pid_alive(p):
            return
        time.sleep(0.2)
    kill_pid(p)
    (bot_dir(bot_id) / "bot.pid").unlink(missing_ok=True)


def save_config(bot_id, changes):
    """Persist settings, and apply them live if the engine is running."""
    bot = registry.get_bot(bot_id)
    cfg_cls = registry.strategy(bot["strategy"])[1]
    cfg = cfg_cls.from_dict({**bot["config"], **changes}).to_dict()
    db.x("UPDATE bots SET config = ? WHERE id = ?", (db.dumps(cfg), bot_id))
    diff = [f"{k} {bot['config'].get(k)} → {v}" for k, v in cfg.items() if bot["config"].get(k) != v
            and not (isinstance(v, (list, tuple)) and list(v) == list(bot["config"].get(k) or []))]
    if diff:
        registry.add_change(bot_id, "Settings changed: " + ", ".join(diff), "settings")
    if pid_alive(pid(bot_id)):
        send(bot_id, "config", config=changes)
    return cfg


def reset(bot_id, bankroll=None):
    """Fresh paper account. Done by the engine itself when it is running."""
    acc = registry.get_bot(bot_id)["account"] + 1
    registry.add_change(bot_id, f"New paper account #{acc}" + (f" (bankroll ${float(bankroll):,.0f})" if bankroll else "")
                        + "; earlier results stay under the old account.", "account")
    if pid_alive(pid(bot_id)):
        send(bot_id, "reset", bankroll=bankroll)
        return
    bot = registry.get_bot(bot_id)
    if bankroll is not None:
        save_config(bot_id, {"bankroll": float(bankroll)})
    from .strategies.market_maker import new_account
    new_account(bot_id, bankroll if bankroll is not None else bot["config"].get("bankroll", 1000.0))
    (bot_dir(bot_id) / "state.json").unlink(missing_ok=True)


def reset_day(bot_id):
    """Clear today's loss so the daily loss limit counts from the current value (the bot trades again). Logged in
    the bot's history; the engine restores it from that entry after a restart."""
    cfg_cls = registry.strategy(registry.get_bot(bot_id)["strategy"])[1]
    if not hasattr(cfg_cls, "max_daily_loss_frac"):
        raise ValueError("this strategy has no daily loss limit")
    s = read_state(bot_id) or {}
    dl = (s.get("account") or {}).get("daily_limit") or {}
    lost = f" (${dl['lost']:,.2f} lost today)" if dl.get("lost") else ""
    registry.add_change(bot_id, f"Daily loss limit reset{lost}: today's loss now counts from the current value.",
                        "daily_reset")
    if pid_alive(pid(bot_id)):
        send(bot_id, "reset_day")


def equity(bot_id, account=None, since=0.0, max_points=2000):
    """[(ts, portfolio value)] of the current (or given) paper account, thinned to max_points."""
    if account is None:
        account = registry.get_bot(bot_id)["account"]
    rows = db.q("SELECT ts, v FROM bot_equity WHERE bot_id = ? AND account = ? AND ts >= ? ORDER BY ts",
                (bot_id, account, since))
    if len(rows) > max_points:
        k = len(rows) // max_points + 1
        rows = rows[::k] + [rows[-1]]
    return [(r["ts"], r["v"]) for r in rows]


STAGES = [
    ("coinbase", "ms", "Coinbase tick: exchange time → received"),
    ("chainlink", "ms", "Chainlink tick: oracle time → received"),
    ("clob", "ms", "Polymarket book/trade event: exchange time → received"),
    ("decide_us", "µs", "Message received → new bids decided (parse + model + quote)"),
    ("e2e_coinbase", "ms", "End to end: Coinbase exchange time → bids decided"),
    ("e2e_book", "ms", "End to end: Polymarket event time → bids decided"),
    ("loop", "ms", "Event-loop stall per 50 ms tick (0 = never blocked)"),
]


def latency_report(bot_id):
    """Timing of every stage of the bot's reaction, from the running engine's own samples."""
    s = read_state(bot_id)
    if not s or "lat_stats" not in s:
        raise RuntimeError("no measurements yet: start the bot and let it run a few minutes")
    up = max(float(s.get("uptime_s") or 0), 1e-9)
    c, t, cfg, stats = s.get("counts") or {}, s.get("totals") or {}, s.get("cfg") or {}, s["lat_stats"]
    return {
        "bot_id": bot_id, "mode": s.get("mode", "paper"), "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s": round(up, 1), "clock_offset_ms": s.get("clock_offset_ms"),
        "stages": {k: {"unit": u, "what": w, **{kk: round(vv, 3) for kk, vv in stats[k].items()}}
                   for k, u, w in STAGES if k in stats},
        "order_send_ms": {"what": "Order/cancel: decision → live on the exchange", "value": cfg.get("latency_ms"),
                          "source": "simulated (config), not measured"},
        "rates_per_min": {"orders_placed": c.get("orders", 0) / up * 60, "cancels": c.get("cancels", 0) / up * 60,
                          "fills": t.get("fills", 0) / up * 60, "quote_pulls_on_jumps": c.get("jumps", 0) / up * 60},
    }

"""Bots: list, create, start/stop, settings, paper account, live state and history."""
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import db
from ...bots import control, registry

router = APIRouter(prefix="/api", tags=["bots"])


def _bot(bot_id):
    try:
        return registry.get_bot(bot_id)
    except KeyError:
        raise HTTPException(404, f"bot '{bot_id}' not found")


def card(b):
    s = control.read_state(b["id"]) or {}
    a = s.get("account") or {}
    t = s.get("totals") or {}
    return {**b, "running": control.is_running(b["id"], s), "strategy_label": registry.STRATEGIES.get(b["strategy"], {}).get("label", b["strategy"]),
            "portfolio": a.get("portfolio"), "pnl_total": a.get("pnl_total"), "pnl_today": a.get("pnl_today"),
            "windows": a.get("windows"), "rebates": t.get("rebate_est"), "bankroll": a.get("bankroll"),
            "fills": t.get("fills"), "maker_fills": t.get("maker_fills"), "taker_fills": t.get("taker_fills"),
            "volume": t.get("volume"), "fees_paid": t.get("fees_paid"),
            "windows_done": s.get("windows_done"), "windows_won": s.get("windows_won"),
            "live": {**{k: v for k, v in s["live"].items() if k not in ("windows", "log", "pnl_series")},
                     "pnl_series_": s["live"].get("pnl_series") or []} if s.get("live") else None}


@router.get("/strategies")
def strategies():
    return [{"id": k, "label": v["label"], "description": v["description"], "defaults": registry.default_config(k)}
            for k, v in registry.STRATEGIES.items()]


SPARK_S = 7 * 86400      # bot cards show the paper account's value over this range


@router.get("/bots")
def list_bots():
    since = time.time() - SPARK_S
    out = []
    for b in registry.list_bots():
        c = card(b)
        if c["live"]:      # live bots: the real account's settled PnL, not the paper twin's portfolio
            pts = [p for p in c["live"].pop("pnl_series_", []) if p[0] >= since][-167:]
            c["spark"] = pts + [[time.time(), c["live"]["realized"]]]
        else:
            c["spark"] = control.equity(b["id"], account=b["account"], since=since, max_points=168)
        out.append(c)
    return out


class NewBot(BaseModel):
    name: str
    strategy: str = "market_maker"
    config: dict = {}


@router.post("/bots")
def create_bot(body: NewBot):
    try:
        return card(registry.create_bot(body.name, body.strategy, body.config))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/bots/{bot_id}")
def get_bot(bot_id: str):
    return card(_bot(bot_id))


@router.get("/bots/{bot_id}/state")
def state(bot_id: str):
    s = control.read_state(bot_id)
    return {"running": control.is_running(bot_id, s), "state": s, "now": time.time()}


@router.get("/bots/{bot_id}/equity")
def equity(bot_id: str, since: float = 0.0):
    return {"points": control.equity(bot_id, since=since)}


@router.get("/bots/{bot_id}/history")
def history(bot_id: str, limit: int = 2000):
    b = _bot(bot_id)
    rows = db.q("SELECT data FROM bot_windows WHERE bot_id = ? AND account = ? ORDER BY start DESC LIMIT ?",
                (bot_id, b["account"], limit))
    return {"windows": [db.loads(r["data"], {}) for r in rows]}


@router.get("/bots/{bot_id}/changes")
def changes(bot_id: str):
    b = _bot(bot_id)
    s = registry.STRATEGIES.get(b["strategy"], {})
    return {"strategy": {"id": b["strategy"], "label": s.get("label", b["strategy"]), "description": s.get("description", "")},
            "changes": registry.changes(bot_id)}


class Note(BaseModel):
    text: str


@router.post("/bots/{bot_id}/changes")
def add_note(bot_id: str, body: Note):
    _bot(bot_id)
    if not body.text.strip():
        raise HTTPException(400, "Write something first.")
    registry.add_change(bot_id, body.text.strip(), "note")
    return {"ok": True}


@router.post("/bots/{bot_id}/start")
def start(bot_id: str):
    _bot(bot_id)
    started = control.start(bot_id)
    for _ in range(40):                     # wait until the engine publishes its first state
        s = control.read_state(bot_id)
        if s and s.get("running") and time.time() - s.get("ts", 0) < 2:
            break
        time.sleep(0.25)
    return {"started": started}


@router.post("/bots/{bot_id}/stop")
def stop(bot_id: str):
    control.stop(bot_id)
    return {"ok": True}


@router.put("/bots/{bot_id}/config")
def save_config(bot_id: str, changes: dict):
    _bot(bot_id)
    try:
        return control.save_config(bot_id, changes)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))


class Reset(BaseModel):
    bankroll: float | None = None


@router.post("/bots/{bot_id}/reset")
def reset(bot_id: str, body: Reset):
    _bot(bot_id)
    control.reset(bot_id, body.bankroll)
    return {"ok": True}


@router.post("/bots/{bot_id}/reset-day")
def reset_day(bot_id: str):
    _bot(bot_id)
    try:
        control.reset_day(bot_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class BotPatch(BaseModel):
    name: str | None = None
    notes: str | None = None


@router.patch("/bots/{bot_id}")
def patch_bot(bot_id: str, body: BotPatch):
    _bot(bot_id)
    return card(registry.update_bot(bot_id, **{k: v for k, v in body.model_dump().items() if v is not None}))


@router.delete("/bots/{bot_id}")
def delete_bot(bot_id: str):
    _bot(bot_id)
    if control.is_running(bot_id):
        raise HTTPException(409, "Stop the bot first.")
    registry.delete_bot(bot_id)
    return {"ok": True}


@router.get("/bots/{bot_id}/latency")
def latency(bot_id: str):
    try:
        return control.latency_report(bot_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))

"""Live manual trading. Orders go over the /ws/trade websocket (lowest latency); REST mirrors exist for
scripts and as a fallback. Market data for the Trade screen is streamed by the browser directly from the
exchanges - the server only handles the account."""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..state import hub
from ... import db
from ...polymarket.markets import fetch_window_prices, slug_start
from ...trading.gateway import gateway

router = APIRouter(tags=["trading"])


@router.get("/api/trade/status")
def status():
    return {**gateway.status(), "clock_offset_ms": hub.clock_offset_ms}


@router.get("/api/trade/markets")
def markets():
    return {"markets": gateway.window_list(), "now": time.time()}


_OFFICIAL = {}     # slug -> Polymarket's {"priceToBeat", "finalPrice"} once published (they never change)


@router.get("/api/trade/price-to-beat/{slug}")
def price_to_beat(slug: str):
    """Price to beat (open) and final price (close) of a window = Chainlink's 60 s TWAP at its start / end, which
    is what it settles on. Polymarket's own numbers (event metadata) once published - the price to beat only
    after the previous window resolved, often minutes in - else our TWAP from the server's always-on Chainlink
    stream (the browser has just a ~60 s backlog after (re)connecting)."""
    start = slug_start(slug)
    if start is None:
        return {}
    end = start + int(slug.split("-")[2][:-1]) * 60
    off = _OFFICIAL.get(slug, {})
    if "finalPrice" not in off and time.time() > start:
        try:
            off = fetch_window_prices(slug)
            if off:
                _OFFICIAL[slug] = off
        except Exception:
            pass
    op = off.get("priceToBeat")
    out = {"open": op if op is not None else hub.twap("BTC", start), "close": off.get("finalPrice"),
           "source": "polymarket" if op is not None else "chainlink twap"}
    if out["close"] is None and time.time() >= end:
        out["close"] = hub.twap("BTC", end)
    return out if out["open"] is not None else {}


class Arm(BaseModel):
    armed: bool


@router.post("/api/trade/arm")
def arm(body: Arm):
    if body.armed and not gateway.enabled:
        raise HTTPException(409, gateway.reason or "trading unavailable")
    gateway.armed = body.armed
    gateway.publish({"type": "status", "status": gateway.status()})
    return {"armed": gateway.armed}


@router.post("/api/trade/setup-approvals")
async def setup_approvals():
    """Sends the wallet's missing trading approvals (gasless). Only ever called from the UI button."""
    try:
        return await asyncio.get_running_loop().run_in_executor(gateway.pool, gateway.setup_approvals)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/api/trade/account")
def account():
    return {"orders": list(gateway.open_orders.values()), "positions": gateway.positions,
            "usdc": gateway.balance, "tokens": gateway.token_balances}


@router.get("/api/trade/history")
def history(limit: int = 100):
    return {"orders": db.q("SELECT order_id, ts, origin, slug, outcome, side, order_type, price, size, amount, status, "
                           "filled, latency_ms, error FROM live_orders ORDER BY ts DESC LIMIT ?", (limit,)),
            "trades": db.q("SELECT id, ts, order_id, slug, outcome, side, price, size, status FROM live_trades "
                           "ORDER BY ts DESC LIMIT ?", (limit,))}


@router.post("/api/trade/order")
async def order(req: dict):
    try:
        return await asyncio.get_running_loop().run_in_executor(gateway.pool, gateway.place, req)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.delete("/api/trade/order/{order_id}")
async def cancel(order_id: str):
    return await asyncio.get_running_loop().run_in_executor(gateway.pool, gateway.cancel, order_id)


@router.post("/api/trade/cancel-all")
async def cancel_all():
    return await asyncio.get_running_loop().run_in_executor(gateway.pool, gateway.cancel_all)


@router.websocket("/ws/trade")
async def trade_ws(ws: WebSocket):
    """Client -> server: {"op": "order", "cid", ...order fields} | {"op": "cancel", "cid", "id"} |
    {"op": "cancel_all", "cid"} | {"op": "ping", "t"}.  Server -> client: ack / pong / pushed account events."""
    await ws.accept()
    q = asyncio.Queue()
    gateway.subscribers.add(q)
    loop = asyncio.get_running_loop()
    await ws.send_text(json.dumps({"type": "hello", "status": {**gateway.status(), "clock_offset_ms": hub.clock_offset_ms},
                                   "markets": gateway.window_list(),
                                   "orders": list(gateway.open_orders.values()), "positions": gateway.positions},
                                  default=str))

    async def pump():
        while True:
            await ws.send_text(await q.get())

    async def run(cid, fn, *args):
        try:
            res = await loop.run_in_executor(gateway.pool, fn, *args)
            msg = {"type": "ack", "cid": cid, "ok": True, "result": res}
        except Exception as e:
            msg = {"type": "ack", "cid": cid, "ok": False, "error": str(e)}
        await q.put(json.dumps(msg, default=str))

    sender = asyncio.create_task(pump())
    try:
        while True:
            m = json.loads(await ws.receive_text())
            op, cid = m.get("op"), m.get("cid")
            if op == "ping":
                await ws.send_text(json.dumps({"type": "pong", "t": m.get("t")}))
            elif op == "order":
                asyncio.create_task(run(cid, gateway.place, m))
            elif op == "cancel":
                asyncio.create_task(run(cid, gateway.cancel, m["id"]))
            elif op == "cancel_all":
                asyncio.create_task(run(cid, gateway.cancel_all))
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        gateway.subscribers.discard(q)

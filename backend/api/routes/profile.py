"""Our Polymarket profile (the trading wallet's public profile): identity, stats, P/L history, positions, activity.
The address is the gateway's (already public in /api/trade/status); nothing here reads .env.live."""
from fastapi import APIRouter, HTTPException

from ...polymarket import profile as pm
from ...trading.gateway import gateway

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _wallet():
    if not gateway.wallet:
        raise HTTPException(404, gateway.reason or "No trading wallet configured")
    return gateway.wallet


def _call(fn, *a):
    try:
        return fn(*a)
    except RuntimeError as e:
        raise HTTPException(502, f"Polymarket: {e}")


@router.get("")
def summary():
    w = _wallet()
    return {**pm.summary(w), "cash": gateway.balance}


@router.get("/pnl")
def pnl(interval: str = "1w"):
    if interval not in pm.PNL_INTERVALS:
        raise HTTPException(400, f"interval must be one of {', '.join(pm.PNL_INTERVALS)}")
    return _call(pm.pnl, _wallet(), interval)


@router.get("/positions")
def positions(status: str = "open", cursor: str | None = None):
    if status not in ("open", "closed"):
        raise HTTPException(400, "status must be open or closed")
    return _call(pm.positions, _wallet(), status, cursor)


@router.get("/activity")
def activity(cursor: str | None = None):
    return _call(pm.activity, _wallet(), cursor)

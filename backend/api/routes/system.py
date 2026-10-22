"""App-wide status and background jobs."""
from fastapi import APIRouter, HTTPException

from ..state import hub, jobs
from ...trading.gateway import gateway

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/status")
def status():
    lat = hub.latency_summary()
    return {"jobs": [j.to_dict() for j in jobs.jobs[-8:]],
            "live": {"rtds": hub.status["rtds"], "feed_p50": lat["feed_p50"], "watching": list(hub.watches)},
            "trading": {"enabled": gateway.enabled, "armed": gateway.armed, "usdc": gateway.balance,
                        "reason": gateway.reason}}


@router.get("/jobs")
def list_jobs():
    return [j.to_dict() for j in jobs.jobs]


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "job not found")
    j.cancel()
    return {"ok": True}


@router.post("/jobs/clear")
def clear_jobs():
    jobs.clear_finished()
    return {"ok": True}

"""Public Polymarket profile of a wallet (the Profile page shows our trading account's): Gamma public-profile plus
Data API v2 (user-stats, user-pnl, leaderboard, positions, activity). All public, read-only; answers are cached for a
few seconds so page polling never hammers Polymarket. v1 hosts user-pnl-api / lb-api answer 410 (retired)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..config import DATA_API, GAMMA

V2 = f"{DATA_API}/v2"
PNL_INTERVALS = ("1d", "1w", "1m", "all")
# user-pnl point fields kept for the chart and the breakdown (cumulative, all-time, at each point)
PNL_FIELDS = ("economic_pnl", "trade_pnl", "fees_paid", "maker_rebate", "taker_rebate", "reward_income",
              "realized_pnl", "unrealized_pnl", "volume", "trade_count")

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"
_POOL = ThreadPoolExecutor(6, thread_name_prefix="profile")
_cache, _lock = {}, threading.Lock()


def _get(url, params, ttl):
    """GET JSON with a per-URL cache of `ttl` seconds; one retry; v2 payloads are unwrapped from `data`."""
    key = (url, tuple(sorted(params.items())))
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    last = None
    for i in range(2):
        try:
            r = HTTP.get(url, params=params, timeout=10)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(0.5)
                continue
            r.raise_for_status()
            js = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            last = str(e)
            time.sleep(0.3 * (i + 1))
    else:
        if hit:                       # Polymarket hiccup: serve the last good answer
            return hit[1]
        raise RuntimeError(f"{url.rsplit('/', 1)[-1]}: {last}")
    with _lock:
        _cache[key] = (time.time(), js)
    return js


def _data(js):
    return js.get("data") if isinstance(js, dict) and "data" in js else js


def summary(wallet):
    """Identity, all-time stats and rank. Each part fails on its own (missing parts come back as None)."""
    jobs = {
        "profile": _POOL.submit(_get, f"{GAMMA}/public-profile", {"address": wallet}, 300),
        "stats": _POOL.submit(_get, f"{V2}/user-stats", {"user": wallet}, 30),
        "rank": _POOL.submit(_get, f"{V2}/leaderboard", {"user": wallet, "timePeriod": "all"}, 120),
    }
    out, errors = {}, []
    for k, f in jobs.items():
        try:
            out[k] = _data(f.result())
        except Exception as e:
            out[k] = None
            errors.append(f"{k}: {e}")
    p, s = out["profile"] or {}, out["stats"] or {}
    return {
        "wallet": wallet,
        "name": p.get("name") or (out["rank"] or {}).get("user_name"),
        "pseudonym": p.get("pseudonym"), "bio": (p.get("bio") or "").strip(), "image": p.get("profileImage"),
        "x_username": p.get("xUsername") or (out["rank"] or {}).get("x_username") or None,
        "verified": bool(p.get("verifiedBadge")), "created": p.get("createdAt"),
        "taker_tier": p.get("takerTierName"), "weighted_volume": p.get("weightedVolume"),
        "join_date": s.get("join_date"), "views": s.get("views"), "markets_traded": s.get("trades"),
        "biggest_win": s.get("biggest_win"),
        "all_time": {k: (s.get("all_time_pnl") or {}).get(k) for k in PNL_FIELDS} if s.get("all_time_pnl") else None,
        "rank": out["rank"], "errors": errors,
    }


def pnl(wallet, interval):
    """P/L over time: [timestamp, economic_pnl] points plus the first/last full point for the period's breakdown."""
    params = {"user": wallet, "interval": interval}
    if interval == "all":
        params["fidelity"] = "1d"
    pts = (_data(_get(f"{V2}/user-pnl", params, 60)) or {}).get("points") or []
    keep = lambda p: {k: p.get(k) for k in PNL_FIELDS}
    return {"interval": interval, "points": [[p["timestamp"], p.get("economic_pnl")] for p in pts],
            "first": keep(pts[0]) if pts else None, "last": keep(pts[-1]) if pts else None}


def positions(wallet, status, cursor=None, limit=50):
    params = {"user": wallet, "status": status, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    js = _get(f"{V2}/positions", params, 15)
    return {"rows": _data(js) or [], "next": ((js or {}).get("pagination") or {}).get("next_cursor") if isinstance(js, dict) else None}


def activity(wallet, cursor=None, limit=50):
    params = {"user": wallet, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    js = _get(f"{V2}/activity", params, 15)
    return {"rows": _data(js) or [], "next": ((js or {}).get("pagination") or {}).get("next_cursor") if isinstance(js, dict) else None}

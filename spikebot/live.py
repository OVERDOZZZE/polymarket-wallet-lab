"""Real orders for the `live` / `live-dry` variants (paper mode never imports this file).

Keys stay where the project's rules put them: orders go through the Wallet Lab app's trading Gateway
(backend/trading/gateway.py, the only code that reads .env.live), so live mode runs from the app's folder on the
Dublin server. Each signal that passes the strategy's filters also sends one fill-and-kill market BUY of
LIVE_ORDER_USD, worst price = signal ask + SLIPPAGE, right away (Polymarket's own taker delay applies). Exact
spend and shares come from the order response. A buy is refused once realized live loss + cost still open +
this order would pass LIVE_BUDGET_USD, so the live account can never lose more than that."""
import asyncio
import time

from . import config as C


class LiveTrader:
    def __init__(self, ledger, log):
        self.ledger, self.log = ledger, log
        self.gw = None
        self.inflight = 0.0

    def start(self, loop):
        if C.LIVE_DRY:
            self.log.info("LIVE DRY RUN: orders are simulated, nothing is sent")
            return
        from backend.trading.gateway import Gateway
        self.gw = Gateway()
        self.gw.armed = True        # running the live variant is the arming
        self.gw.light = True        # poll USDC only: nothing competes with the order path
        self.gw.start(loop)

    def status(self):
        if C.LIVE_DRY:
            return {"dry": True}
        gw = self.gw
        return {"dry": False, "enabled": bool(gw and gw.enabled), "reason": gw.reason if gw else "",
                "usdc": gw.balance if gw else None, "inflight": self.inflight, **self.ledger.live_risk()}

    def refuse(self, slug):
        """Why a live buy may not go now, or None."""
        r = self.ledger.live_risk()
        if not C.LIVE_DRY:
            gw = self.gw
            if gw is None or not gw.enabled:
                return "no_gateway"
            if slug not in gw.markets:
                return "market_not_loaded"
            if gw.balance is not None and gw.balance < C.LIVE_ORDER_USD + self.inflight:
                return "no_cash"
        if max(0.0, -r["realized"]) + r["open_cost"] + self.inflight + C.LIVE_ORDER_USD > C.LIVE_BUDGET_USD + 1e-9:
            return "budget"
        return None

    async def buy(self, slug, side, ask, move, tin, signal_ts):
        limit = round(min(ask + C.SLIPPAGE, 0.99), 2)
        why = self.refuse(slug)
        row = {"ts": time.time(), "signal_ts": signal_ts, "slug": slug, "side": side, "tin": tin, "move_bps": move,
               "ask_signal": ask, "limit_px": limit, "usd": C.LIVE_ORDER_USD}
        if why:
            self.ledger.live_order({**row, "result": why})
            return
        self.inflight += C.LIVE_ORDER_USD
        try:
            if C.LIVE_DRY:
                await asyncio.sleep(0.5)
                r = {"ok": False, "error": "dry run", "total_ms": 500}
            else:
                req = {"slug": slug, "outcome": side, "side": "BUY", "type": "market", "amount": C.LIVE_ORDER_USD,
                       "price": limit, "origin": f"spikebot-{C.VARIANT}"}
                loop = asyncio.get_running_loop()
                try:
                    r = await loop.run_in_executor(self.gw.pool, self.gw.place, req)
                except Exception as e:
                    r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            self.inflight -= C.LIVE_ORDER_USD
        spent = shares = 0.0
        if r.get("ok"):
            try:
                spent, shares = float(r.get("making") or 0), float(r.get("taking") or 0)
            except (TypeError, ValueError):
                pass
        if spent > 0 and shares > 0:
            result = "filled" if spent >= C.LIVE_ORDER_USD - 0.02 else "partial"
        else:
            err = str(r.get("error") or "")
            result = "dry" if C.LIVE_DRY else ("missed" if r.get("ok") or "match" in err.lower() else "error")
        self.ledger.live_order({**row, "result": result, "spent": spent, "shares": shares,
                                "order_id": r.get("order_id"), "ms": r.get("total_ms"), "error": r.get("error")})
        self.log.info("LIVE %s %s %s ask %.3f limit %.2f -> %s spent %.2f shares %.2f (%s ms)%s", result.upper(), slug,
                      side, ask, limit, result, spent, shares, r.get("total_ms"),
                      f" error: {r.get('error')}" if result == "error" else "")

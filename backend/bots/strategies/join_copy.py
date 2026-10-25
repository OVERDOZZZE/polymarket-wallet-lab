"""Join-after-fill copy bot: watches a target wallet live and, the moment it gets a fill on a BTC Up/Down
window, posts our own bid at that same price on the same outcome (their order usually still rests there:
most of their fills are partials). Unfilled copies expire after `order_ttl_s`; whatever fills is held to
resolution.

Target trades come from Polymarket's RTDS activity stream (~0.3 s after the match), filtered by wallet.
Backtest on 300 machinelearned windows (2026-10-21): -0.4% to -5.5% per $ depending on queue position,
versus +5.3% on their own maker fills - their edge is the timing of the bid, which a copy arrives after.
Paper trading on the PaperBot engine (same books, queue-aware fill simulation, settlement, persistence).
"""
import asyncio
import collections
import dataclasses
import json
import time

import orjson

from .market_maker import WS_CLOSE_S, BotConfig, PaperBot, _floor_tick
from ...config import RTDS_WS

ML_WALLET = "0x86b14772a4da70f3e6d5eee06933ebef143efc87"


@dataclasses.dataclass
class JoinConfig(BotConfig):
    bankroll: float = 1000.0
    windows: tuple = (5,)                 # windows to load books for (the target trades BTC 5m)
    target_wallet: str = ML_WALLET
    size_pct: float = 0.5                 # copy this fraction of each of their fills (min 5 shares)
    order_ttl_s: float = 60.0             # cancel a copy bid that hasn't filled after this long
    copy_takers: bool = True              # also copy their taker buys (posted 1 tick under the ask)
    max_window_cost: float = 250.0
    max_side_shares: float = 600.0
    start_delay_s: float = 0.0
    stop_before_end_s: float = 2.0
    jump_bps: float = 1e6                 # never pull copies on Coinbase jumps
    taker_enabled: bool = False
    min_price: float = 0.01
    max_price: float = 0.99


class JoinCopyBot(PaperBot):
    CONFIG = JoinConfig
    STRATEGY = "join_copy"

    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self.copies = collections.defaultdict(list)   # slug -> [{"side", "px", "left", "exp_ms"}]
        self.lat["copy"] = collections.deque(maxlen=5000)   # their match time -> our bid decided (ms)
        self.status["target"] = "stopped"

    async def _main(self):
        asyncio.get_running_loop().create_task(self._watch())
        await super()._main()

    # ------------------------------------------------------------------ target feed
    async def _watch(self):
        import websockets
        backoff = 1
        while True:
            try:
                self.status["target"] = "connecting"
                async with websockets.connect(RTDS_WS, ping_interval=10, ping_timeout=10, max_size=None,
                                              open_timeout=10, close_timeout=WS_CLOSE_S) as ws:
                    await ws.send(json.dumps({"action": "subscribe",
                                              "subscriptions": [{"topic": "activity", "type": "trades"}]}))
                    self.status["target"] = "connected"
                    backoff = 1
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        if self.cfg.target_wallet in raw.lower():         # cheap pre-filter of the firehose
                            self._on_target(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status["target"] = "reconnecting"
                self._err(f"target feed: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20)

    def _on_target(self, raw):
        try:
            self._on_target_msg(orjson.loads(raw))
        except ValueError:
            return

    def _on_target_msg(self, d):
        now = self.clock() * 1000.0
        p = d.get("payload") or {}
        if str(p.get("proxyWallet") or "").lower() != self.cfg.target_wallet or str(p.get("side")).upper() != "BUY":
            return
        m = self.markets.get(p.get("slug") or "")
        side = str(p.get("outcome") or "").lower()
        if m is None or side not in ("up", "down"):
            self.counts["target_fills_skipped"] += 1
            return
        self.counts["target_fills"] += 1
        price, size = float(p.get("price") or 0), float(p.get("size") or 0)
        ask = m.best_ask(side)
        if ask is not None and price >= ask - 1e-9:                # they took liquidity
            if not self.cfg.copy_takers:
                return
            price = ask - m.tick                                   # post-only: rest just under the ask
        px = round(_floor_tick(price, m.tick), 4)
        if not (self.cfg.min_price <= px <= self.cfg.max_price):
            return
        sz = max(m.min_size, round(size * self.cfg.size_pct, 2))
        for c in self.copies[m.slug]:                               # merge with an existing copy at that price
            if c["side"] == side and abs(c["px"] - px) < 1e-9:
                c["left"] += sz
                c["exp_ms"] = now + self.cfg.order_ttl_s * 1000
                break
        else:
            self.copies[m.slug].append({"side": side, "px": px, "left": sz, "exp_ms": now + self.cfg.order_ttl_s * 1000})
        ts = p.get("timestamp")
        if ts and self.clock_offset_ms is not None:
            self.lat["copy"].append(self._corr(now) - float(ts) * 1000)
        self._requote(m, now, force=True)

    # ------------------------------------------------------------------ quoting = the live copies
    def _side_quotes(self, m, side, fair, now_s, imb):
        now = now_s * 1000
        live = [c for c in self.copies.get(m.slug, []) if c["exp_ms"] > now and c["left"] >= m.min_size - 1e-9]
        self.copies[m.slug] = live
        cost_cap = m.total_cost() >= self.cfg.max_window_cost
        if cost_cap or m.shares[side] >= self.cfg.max_side_shares:
            return []
        ask = m.best_ask(side)
        want = {}
        for c in live:
            if c["side"] != side:
                continue
            px = c["px"] if ask is None else min(c["px"], round(ask - m.tick, 4))
            want[px] = want.get(px, 0.0) + c["left"]
        return sorted(want.items(), key=lambda x: -x[0])

    def _fill(self, m, o, shares, price, now, maker):
        left = shares
        # a copy may rest below its own price (clamped under the ask), so match any copy at or above the fill
        for c in sorted(self.copies.get(m.slug, []), key=lambda c: -c["px"]):
            if c["side"] == o.side and c["px"] >= o.price - 1e-9 and left > 0:
                take = min(c["left"], left)
                c["left"] -= take
                left -= take
        super()._fill(m, o, shares, price, now, maker)

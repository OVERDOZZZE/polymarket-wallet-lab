"""CrouchPredictions, followed properly (strategy "crouch_v2"). Built 2026-10-21 from its 5,598 BTC 5m/15m
fills of 14:00-19:00 UTC that day, split into maker/taker with the data-api `takerOnly` flag, and compared
window by window with the older reconstruction (market_maker / bot crouch-mm, kept unchanged):

  * ONE resting bid per side, at the best bid (it is the top of the book in ~80% of its maker fills and only
    96% of the sweeps that hit it hit a single price of its), capped at fair value. No 3-level ladder.
  * Order size depends on price: ~10-25 shares in the middle, much larger at the extremes (50-115 shares
    below 5c and above 90c). Bids anywhere from 1c to 99c (7-19% of its $ is above 95c).
  * It also TAKES: 8% of its $ on 5m windows, 34% on 15m. Fixed 20 shares (5m) / 30 shares (15m), mostly
    right after fair value moved towards that side (68% of its takes), paying about fair.
  * It keeps trading to the close: median spend $641 per 5m window (p90 $1,273), $388 per 15m window;
    first fill ~6 s after the open.
  * Never sells or merges; holds to settlement; moderate imbalance (|Up - Down| / total ~0.14 on 5m).

Calibrated from fills only (public data has no order placements or cancels), so order sizes are the fill
runs' typical totals and the taker trigger is an approximation. Re-check it against the trader with the
same window-by-window comparison before trusting it. Everything else (feeds, books, queue-aware paper fills,
settlement, rebates, persistence) is the PaperBot engine.
"""
import collections
import dataclasses

from .market_maker import BotConfig, Order, PaperBot, _floor_tick


@dataclasses.dataclass
class CrouchConfig(BotConfig):
    bankroll: float = 3000.0              # it runs $640 (5m) + $390 (15m) at once, more in busy windows
    windows: tuple = (5, 15)
    levels: int = 1
    base_edge: float = 0.0                # join the best bid as long as it is not above fair
    join_only: bool = True
    skew_per_100: float = 0.005
    max_side_shares: float = 3000.0
    max_imbalance: float = 600.0
    max_window_cost: float = 1300.0       # 5m windows (its p90)
    max_window_cost_15: float = 700.0     # 15m windows (its p90)
    min_price: float = 0.01
    max_price: float = 0.99
    start_delay_s: float = 4.0
    stop_before_end_s: float = 1.0
    size_pct: float = 1.0                 # fraction of its order sizes
    price_edges: tuple = (0.05, 0.12, 0.3, 0.5, 0.7, 0.9, 0.95)
    sizes_5m: tuple = (115, 15, 10, 15, 20, 25, 60, 115)    # shares per price band (between the edges)
    sizes_15m: tuple = (45, 30, 15, 20, 25, 15, 45, 50)
    taker_enabled: bool = True
    taker_size: float = 20.0              # 5m windows
    taker_size_15: float = 30.0           # 15m windows
    taker_mom: float = 0.03               # take a side after its fair value rose this much ...
    taker_mom_s: float = 5.0              # ... within this many seconds
    taker_slack: float = 0.01             # pay at most fair + this
    taker_cooldown_ms: float = 20000.0    # per side
    taker_mode: str = "momentum"          # momentum (above) | edge (engine: take when fair - ask >= taker_edge)


class CrouchBot(PaperBot):
    CONFIG = CrouchConfig
    STRATEGY = "crouch_v2"

    def _size(self, m, px):
        cfg = self.cfg
        table = cfg.sizes_5m if m.minutes == 5 else cfg.sizes_15m
        i = min(sum(1 for e in cfg.price_edges if px >= e), len(table) - 1)
        return max(float(round(table[i] * cfg.size_pct)), m.min_size)

    def _cost_cap(self, m):
        return self.cfg.max_window_cost if m.minutes == 5 else self.cfg.max_window_cost_15

    def _side_quotes(self, m, side, fair, now_s, imb):
        cfg = self.cfg
        cost_cap = m.total_cost() >= self._cost_cap(m)
        heavy = (imb > 0) == (side == "up") and abs(imb) > 1e-9
        if (m.shares[side] >= cfg.max_side_shares or (heavy and abs(imb) >= cfg.max_imbalance)
                or (cost_cap and (heavy or abs(imb) < 1e-9))):
            return []
        skew = cfg.skew_per_100 * imb / 100.0
        top = min(fair - cfg.base_edge - (skew if side == "up" else -skew), fair)
        ask, bid = m.best_ask(side), m.best_bid(side)
        if ask is not None:
            top = min(top, ask - m.tick)                # post-only
        if cfg.join_only and bid is not None:
            top = min(top, bid)
        want = []
        for i in range(max(1, cfg.levels)):
            px = round(_floor_tick(top - i * cfg.level_step, m.tick), 4)
            if not cfg.min_price <= px <= cfg.max_price:
                continue
            sz = self._size(m, px)
            if cost_cap:                                # over budget: only rebalance towards flat
                sz = min(sz, abs(imb))
                if sz < m.min_size:
                    continue
            want.append((px, sz))
        return want

    def _maybe_take(self, m, p, now):
        """Buy `taker_size` at the ask of the side whose fair value just rose by `taker_mom`, paying at most
        fair + `taker_slack`. taker_mode "edge" uses the engine's fair-vs-ask rule instead."""
        cfg = self.cfg
        if cfg.taker_mode == "edge":
            if m.total_cost() < self._cost_cap(m):
                PaperBot._maybe_take(self, m, p, now)
            return
        h = m.__dict__.setdefault("fair_hist", collections.deque())
        h.append((now, p))
        win_ms = cfg.taker_mom_s * 1000
        while h and h[0][0] < now - win_ms:
            h.popleft()
        if len(h) < 2 or now - h[0][0] < 0.8 * win_ms:
            return
        move = p - h[0][1]
        size = cfg.taker_size if m.minutes == 5 else cfg.taker_size_15
        if m.total_cost() >= self._cost_cap(m):
            return
        for side, fair, mv in (("up", p, move), ("down", 1 - p, -move)):
            if mv < cfg.taker_mom or now - m.last_taker_ms[side] < cfg.taker_cooldown_ms:
                continue
            if m.shares[side] >= cfg.max_side_shares:
                continue
            ask = m.best_ask(side)
            if ask is None or ask > cfg.max_price or ask > fair + cfg.taker_slack:
                continue
            if self.cash - self._reserved() < ask * size:
                self.counts["cash_limited"] += 1
                continue
            m.last_taker_ms[side] = now
            self._oid += 1
            m.orders.append(Order(self._oid, side, ask, size, 0.0, now, cfg.latency_ms, taker=True))
            self.counts["taker_orders"] += 1

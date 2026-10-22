"""Reconstruction of the machinelearned wallet's strategy (BTC 5m Up/Down), calibrated 2026-10-21 on its
145k historical buys plus the full public trade tape of 300 of its windows (5,443 fills):

  * 88% of its fills are MAKER fills: it rests bids on BOTH sides, at the market (median offset to the
    market price -1c..0c); +1c above it in the first ~10 s, 2-4c below it after ~240 s.
  * It never sells; holds to settlement. ~15 fills and ~$190 per window; first fill ~19 s, last ~220 s.
  * Order size depends on price: ~15 shares under 40c, ~20 at 40-60c, ~25 above 60c (round orders,
    partially filled). Late in the window, cheap sides get small fixed-$ tickets (size = ceil($5 / price)).
  * 12% TAKER fills: small (6-10 sh), paying 2-6c over the market when its fair value says the ask is cheap.
  * A fair-value model matters: ROI rises monotonically with model edge (rank corr +0.93), so bids are capped
    at fair value (Coinbase-led, like the market-maker engine) instead of joining blindly.

Sizing = `size_pct` x their size (Polymarket minimum 5 shares). Everything else (feeds, books, paper fill
simulation with queue position, settlement, rebates, persistence) is the PaperBot engine.
"""
import dataclasses
import math

from .market_maker import BotConfig, PaperBot, _floor_tick


@dataclasses.dataclass
class MLConfig(BotConfig):
    # engine settings, defaults matched to machinelearned
    bankroll: float = 1000.0
    windows: tuple = (5,)                 # it trades BTC 5m only
    levels: int = 1                       # one resting order per side at a time
    level_step: float = 0.02
    base_edge: float = -0.01              # never bid above fair + 1c (its fills sit ~at fair)
    join_only: bool = False               # it improves the bid early in the window
    start_delay_s: float = 3.0
    stop_before_end_s: float = 8.0
    skew_per_100: float = 0.0             # it runs directional inventory (hedge ratio 0.69)
    max_side_shares: float = 400.0
    max_imbalance: float = 300.0
    max_window_cost: float = 250.0        # 25% of the bankroll per window
    min_price: float = 0.01
    max_price: float = 0.97
    taker_enabled: bool = True
    taker_edge: float = 0.08              # it pays 2-6c over the market when it takes, ~2 times per window
    taker_size: float = 5.0
    taker_cooldown_ms: float = 120000.0   # at most one take per side per 2 minutes
    # strategy settings
    size_pct: float = 0.5                 # copy this fraction of its order sizes
    their_sizes: tuple = (15, 20, 25)     # its typical order shares below 40c / 40-60c / above 60c
    their_size_edges: tuple = (0.4, 0.6)
    early_s: float = 10.0                 # first seconds: bid 1 tick above the best bid
    early_improve: float = 0.01
    late_s: float = 240.0                 # after this: bid below the market
    late_offset: float = 0.03
    ticket_usd: float = 5.0               # late cheap-side tickets: ceil(ticket_usd * size_pct / price) shares
    ticket_max_price: float = 0.12
    refill_cooldown_s: float = 13.0       # after a fill, wait this long before bidding that side again
                                          # (it averages one fill per ~13 s; instant refills chase a falling side)


class MLBot(PaperBot):
    CONFIG = MLConfig
    STRATEGY = "ml_maker"

    def _fill(self, m, o, shares, price, now, maker):
        m.__dict__.setdefault("last_fill_ms", {})[o.side] = now
        super()._fill(m, o, shares, price, now, maker)

    def _their_size(self, px):
        cfg = self.cfg
        i = sum(1 for e in cfg.their_size_edges if px >= e)
        return float(cfg.their_sizes[min(i, len(cfg.their_sizes) - 1)])

    def _side_quotes(self, m, side, fair, now_s, imb):
        cfg = self.cfg
        bid, ask = m.best_bid(side), m.best_ask(side)
        if bid is None:
            return []
        t_in = now_s - m.start
        heavy = (imb > 0) == (side == "up") and abs(imb) > 1e-9
        cost_cap = m.total_cost() >= cfg.max_window_cost
        if self._blocked(m, side, imb, heavy, cost_cap):
            return []
        last = m.__dict__.get("last_fill_ms", {}).get(side)
        if last is not None and now_s * 1000 - last < cfg.refill_cooldown_s * 1000:
            return []
        if t_in < cfg.early_s:
            top = bid + cfg.early_improve
        elif t_in >= cfg.late_s:
            top = bid - cfg.late_offset
        else:
            top = bid
        top = min(top, fair - cfg.base_edge)                 # fair-value cap
        if ask is not None:
            top = min(top, ask - m.tick)                     # post-only
        want = []
        for i in range(max(1, cfg.levels)):
            px = round(_floor_tick(top - i * cfg.level_step, m.tick), 4)
            if px < cfg.min_price or px > cfg.max_price:
                continue
            if t_in >= cfg.late_s and px <= cfg.ticket_max_price:
                sz = math.ceil(cfg.ticket_usd * cfg.size_pct / px)
            else:
                sz = round(self._their_size(px) * cfg.size_pct)
            want.append((px, float(max(sz, m.min_size))))
        return want

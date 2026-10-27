"""Stale-quote taker (strategy "stale_taker"), paper only. Built 2026-10-26 from the study's
`reports/TAKER_SPEC.md` (Polymarket Trader Analysis; evidence in its LIVE_NOTES.md sections 4-13).

Edge: after a Binance futures move, Polymarket's 5m makers leave their quotes up for seconds (CrouchPredictions:
65 % of the orders a 0.5 bps move threatens still rest 1 s later). Buying the favoured token at those stale asks
earned +2.9 c/share after fees in the study's backtest at Polymarket's real taker latency (10-22..25, every day
positive), and its fill rule matched fast-value-live's 264 real FAK orders on 96.6 % of them.

Rules, BTC 5m windows:
  * on every Binance USD-M futures trade (receipt time): r = price / (last price at or before now - 1 s) - 1;
  * trigger: |r| >= spike_bps, at least cooldown_s after this window's last order, from start + start_delay_s to
    end - stop_before_end_s, at most max_entries_per_window orders per window;
  * r > 0 buys Up, r < 0 buys Down: a FAK buy of `shares` shares at limit = the token's best ask now
    + slip_ticks ticks, only if min_price <= limit <= max_price;
  * held to settlement. No price model, no edge filter, no exits.

Differences from fast_player (whose paper engine this reuses: FAK fills against the book as it stood at the match
time, liquidity our own paper takers used stays used, fees in shares, settlement):
  * the signal is Binance futures only, and nothing but the move gates an entry;
  * the order reaches the matching engine after a delay drawn from fast-value-live's real FAK latencies
    (LATENCY_Q: p10 247, p50 713, p90 2,529 ms; Polymarket holds marketable orders), not a fixed latency_ms;
  * a fixed share size per order; orders of one window may overlap (no "busy" wait).
Paper only: mode 'live' is ignored (no gateway is created, nothing is ever sent).
"""
import dataclasses
import math
import random

from .fast_player import Fak, FastBot, FastConfig, _st

# fast-value-live's FAK orders (260, 2026-10-23..26): sign + POST - 12 ms one way back, every 5th percentile (ms)
LATENCY_Q = (196, 247, 289, 318, 361, 418, 464, 528, 570, 617, 713, 842, 940, 1048, 1170, 1420, 1641, 1866, 2127,
             2529, 5238)


@dataclasses.dataclass
class StaleTakerConfig(FastConfig):
    bankroll: float = 1000.0
    sources: tuple = ("binance_fut",)
    spike_bps: float = 1.0
    lookback_ms: float = 1000.0
    combine: str = "mean"
    feed_stale_ms: float = 3000.0
    shares: float = 10.0               # per order
    slip_ticks: int = 2                # limit = best ask + this many ticks
    min_price: float = 0.08
    max_price: float = 0.92
    start_delay_s: float = 5.0
    stop_before_end_s: float = 30.0
    max_entries_per_window: int = 10
    cooldown_s: float = 2.0
    latency_dist: bool = True          # draw each order's latency from LATENCY_Q (False: fixed latency_ms)
    latency_ms: float = 713.0
    match_wait_ms: float = 6000.0      # quiet market: fill against the current book this long after placement
    exit_mode: str = "hold"
    max_window_frac: float = 0.20
    max_open_frac: float = 0.50
    max_daily_loss_frac: float = 0.20


def draw_latency(rng):
    """A latency from LATENCY_Q, linear between its 5-percentile points."""
    u = rng.random() * (len(LATENCY_Q) - 1)
    i = int(u)
    j = min(i + 1, len(LATENCY_Q) - 1)
    return LATENCY_Q[i] + (LATENCY_Q[j] - LATENCY_Q[i]) * (u - i)


class StaleTakerBot(FastBot):
    CONFIG = StaleTakerConfig
    STRATEGY = "stale_taker"

    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self.mode = "paper"                                     # never live, whatever the bots row says
        self.live.clear()
        self._rng = random.Random()

    def _latency(self):
        return draw_latency(self._rng) if self.cfg.latency_dist else self.cfg.latency_ms

    def _entry(self, m, side, mv, now, sig):
        """Place the paper FAK buy; returns None, or why not."""
        cfg = self.cfg
        st = _st(m)
        if st["entries"] >= cfg.max_entries_per_window:
            return "max_entries"
        if now - st["last"] < cfg.cooldown_s * 1000:
            return "cooldown"
        if not m.book_ok or now - m.last_book_ms > cfg.feed_stale_ms:
            return "stale_book"
        eq = self.portfolio_value()
        if self.daily_loss_hit(eq):
            return "daily_loss"
        ask = m.best_ask(side)
        if ask is None:
            return "no_ask"
        sig["ask"] = ask
        limit = round(ask + cfg.slip_ticks * m.tick, 6)
        if limit > cfg.max_price + 1e-9:
            return "price_cap"
        if limit < cfg.min_price - 1e-9:
            return "price_floor"
        shares = max(cfg.shares, m.min_size)
        usd = max(shares * limit, 1.0)                          # Polymarket's minimum: min_size shares, $1
        open_cost = sum(mk.total_cost() for mk in self.markets.values() if mk.state != "settled")
        room = min(cfg.max_window_frac * eq - m.total_cost(), cfg.max_open_frac * eq - open_cost,
                   self.cash - self._reserved())
        if usd > room:
            return "risk_limit" if self.cash - self._reserved() >= usd else "cash"
        usd = math.ceil(usd * 100) / 100
        sig["limit"], sig["usd"] = limit, usd
        lat = self._latency()
        self.pending.append(Fak(m, side, True, limit, usd, 0.0, now, lat, self._corr(now) + lat, sig))
        st["entries"] += 1
        st["last"] = now
        self.counts["orders"] += 1
        return None

"""Late-favourite maker (strategy "late_fav"): resting bids at 97-99c on the near-certain side in a window's last
two minutes, held to settlement. Built 2026-10-22 from the old logger PC's 60 h study (data/import/btc5m,
reports/ULTIMATE_REPORT.md section 6) and its 3.17 M wallet fills:

  * every wallet's maker BUYs on the favourite in the last 120 s, held to settlement (no fee for makers):
    95-96c +1.7% per $1 (3.0% of fills lost), 97c +1.5% (2.0%), 98c +0.7% (1.5%), 99c +1.0% (0.02%); the same
    in both halves of the data. By time left: last 30 s +2.1-2.4%, 30-120 s +0.7-0.9%. Earlier: nothing;
  * the taker version (buying the favourite at the ask) earns nothing: the profit is in waiting for sellers
    (holders dumping winning shares early). Profits are steady, losses rare but large (a reversal sweeps the bids).

Rules: in the last `window_s` seconds (until `stop_before_end_s`), if one side's book mid >= fav_min_mid and the
model also gives it >= model_min, rest post-only bids at `prices` (`sizes` shares each) on that side; never above
the best ask minus a tick. Everything is pulled on a fast BTC move (the engine's jump guard, `jump_bps` in 1 s on
Coinbase) - the moments a reversal sweeps the bids - and once the window's cost reaches max_window_cost.

Fills use the engine's queue-aware maker simulation (market_maker.py): we join the back of each level and fill only
after the shares ahead of us traded or were cancelled. That queue is the unknown the wallet data cannot show.
"""
import dataclasses

from .market_maker import BotConfig, PaperBot


@dataclasses.dataclass
class LateFavConfig(BotConfig):
    bankroll: float = 500.0
    windows: tuple = (5,)
    window_s: float = 120.0           # bid only in the last this many seconds
    start_delay_s: float = 0.0
    stop_before_end_s: float = 1.0
    fav_min_mid: float = 0.96         # the favourite: its book mid at least this
    model_min: float = 0.95           # ... and the price model agrees (0 = off)
    prices: tuple = (0.97, 0.98, 0.99)
    sizes: tuple = (30, 40, 60)       # shares per price
    max_window_cost: float = 150.0
    jump_bps: float = 2.5             # pull every bid on a BTC move this big within 1 s ...
    jump_cooldown_ms: float = 3000.0  # ... for this long
    join_only: bool = False
    taker_enabled: bool = False
    latency_ms: float = 50.0          # Dublin


class LateFavBot(PaperBot):
    CONFIG = LateFavConfig
    STRATEGY = "late_fav"

    def _side_quotes(self, m, side, fair, now_s, imb):
        cfg = self.cfg
        if m.end - now_s > cfg.window_s or m.total_cost() >= cfg.max_window_cost:
            return []
        mid = m.mid(side)
        if mid is None or mid < cfg.fav_min_mid:
            return []
        if cfg.model_min > 0:
            p = m.fair if side == "up" else (None if m.fair is None else 1 - m.fair)
            if p is None or p < cfg.model_min:
                return []
        ask = m.best_ask(side)
        room = cfg.max_window_cost - m.total_cost()
        want = []
        for px, sz in zip(cfg.prices, cfg.sizes):
            if ask is not None and px > ask - m.tick + 1e-9:          # post-only: never cross
                continue
            sz = min(float(sz), room / px)
            if sz < m.min_size:
                break
            want.append((round(px, 4), sz))
            room -= px * sz
        return want

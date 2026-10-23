"""Guarded two-sided maker (strategy "guard_maker"): post-only bids on both Up and Down around fair value, earning
the spread and the maker rebate, with the fast player's BTC spike signal used to pull the side about to be
picked off. Built 2026-10-23 ("design 1": defensive two-sided quoting).

Why it can work: Polymarket holds taker orders before matching them, and makers' cancels are not held.
Measured from the Dublin server (2026-10-23):
  * fast-value-live's 63 FAK buys took 207-2670 ms to post (median ~460 ms) and only 9 filled: the stale asks
    were gone by the time they matched;
  * our post-only orders are live on the book ~26 ms after sending (p90 ~35 ms) and our cancels take effect
    ~26 ms after sending (p90 ~31 ms): send -> our +/-5 shares on the market feed, minus the feed's 10 ms
    (scripts/maker_latency_test.py, 20 rounds).
So when BTC jumps, a maker who cancels at once is out of the book well before the takers chasing the same jump
match.

Rules:
  * quoting is the PaperBot engine's (market_maker.py): on each side one ladder of `levels` bids, the top at
    fair - base_edge (fair = book mid + Coinbase lead + model_weight x (model - mid)), never crossing the ask,
    joining (not improving) the best bid when join_only, skewed against inventory, within the inventory and
    $-per-window caps; maker_min_edge keeps every bid at least that far below the pure model;
  * the signal is the fast player's (fast_player.py): the mean move of Coinbase/Kraken/Binance over
    `lookback_ms` >= spike_bps. BTC up -> Down bids are the ones takers will hit (buying Up against them), so
    every Down bid is cancelled at once and Down stays unquoted for `pull_ms` after the last spike tick; BTC down
    -> the same for Up. The favoured side keeps quoting at its (re-priced) fair value;
  * the engine's jump guard (jump_bps in 1 s on Coinbase) still pulls both sides on very large moves;
  * everything is held to settlement.

Simulation (on top of the engine's queue-aware maker fills): orders go live `latency_ms` after placing and
cancels take effect `cancel_latency_ms` after sending; a trade print is judged at its match time (the exchange
timestamp, clock-corrected) rather than when it arrived here, so the feed's ~10 ms never lets a cancel win a race
it would have lost. The taker hold needs no model of its own: prints arrive when the held orders actually match.
"""
import dataclasses

from .fast_player import FastBot
from .market_maker import BotConfig, PaperBot


@dataclasses.dataclass
class GuardMakerConfig(BotConfig):
    bankroll: float = 1000.0
    windows: tuple = (5, 15)
    # quoting
    levels: int = 1
    sizes: tuple = (10,)
    base_edge: float = 0.01
    join_only: bool = True
    skew_per_100: float = 0.02
    maker_min_edge: float = 0.0          # never bid above the pure model's fair value
    start_delay_s: float = 4.0
    stop_before_end_s: float = 10.0
    # risk
    max_side_shares: float = 200.0
    max_imbalance: float = 50.0
    max_window_cost: float = 100.0
    min_price: float = 0.10
    max_price: float = 0.90
    jump_bps: float = 6.0                # both sides out on a move this big within 1 s (Coinbase) ...
    jump_cooldown_ms: float = 1500.0     # ... for this long
    # signal (the fast player's)
    sources: tuple = ("coinbase", "kraken", "binance")
    spike_bps: float = 2.0
    lookback_ms: float = 1000.0
    combine: str = "mean"
    feed_stale_ms: float = 3000.0
    pull_ms: float = 2000.0              # the threatened side stays unquoted this long after the last spike tick
    # simulation (Dublin, measured 2026-10-23: live ~26 ms p50 / ~35 ms p90 after sending, both ways)
    latency_ms: float = 35.0
    cancel_latency_ms: float = 35.0
    taker_enabled: bool = False


def _gs(m):
    """Per-window guard state, kept on the Market."""
    st = m.__dict__.get("gm")
    if st is None:
        st = m.gm = {"pull": {"up": 0.0, "down": 0.0}}
    return st


class GuardMakerBot(FastBot):
    """FastBot supplies the extra exchange feeds and the spike detector; quoting, fills, reservations and the
    snapshot are the PaperBot engine's again."""
    CONFIG = GuardMakerConfig
    STRATEGY = "guard_maker"

    _reserved = PaperBot._reserved

    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self.mode, self.live = "paper", {}            # paper only: FastBot's live order path stays off

    def _cancel_lat(self):
        return self.cfg.cancel_latency_ms

    def _print_ms(self, recv, ts):
        if not ts:
            return recv
        return min(recv, float(ts) - (self.clock_offset_ms or 0.0))

    def _on_ex(self, src, price, recv):
        h = self.ex[src]
        changed = not h or h[-1][1] != price
        super()._on_ex(src, price, recv)              # spike -> _try_enter on every window
        if src == "coinbase" and self.cb["mid"] is not None:
            self._jump_check(self.cb_hist, self.cb["mid"], recv)
        if changed and (src == "coinbase" or (src == "binance" and self.cfg.spot_source != "coinbase")):
            self._requote_all(recv)                   # fair value follows the spot

    def _try_enter(self, m, mv, now):
        """A spike tick: pull every bid on the side the move makes stale, keep the other side quoting."""
        if m.minutes not in self.cfg.windows or m.state not in ("pending", "quoting"):
            return
        threat = "down" if mv > 0 else "up"
        _gs(m)["pull"][threat] = now + self.cfg.pull_ms
        pulled = 0
        for o in m.orders:
            if o.side == threat and not o.taker and o.cancel_ms is None:
                o.cancel_ms = now
                pulled += 1
        if pulled:
            self.counts["guard_cancels"] += pulled
        sp = self.spike
        first = m.slug not in sp["seen"]
        sp["seen"].add(m.slug)
        if first or pulled:
            now_s = now / 1000.0
            self.counts["guard_" + ("pulled" if pulled else "none_resting")] += 1
            self.signals.append({"ts": now_s, "slug": m.slug, "win": m.minutes, "t_left": round(m.end - now_s, 1),
                                 "side": threat, "move": round(mv * 1e4, 2), "spike": sp["id"], "ask": None,
                                 "fair": None, "edge": None, "limit": None, "usd": None,
                                 "result": "pulled" if pulled else "none_resting",
                                 "filled": pulled or None, "avg": None})
        self._requote(m, now, force=True)             # the favoured side re-prices at once

    def _requote(self, m, now, force=False):
        # a fill judged at its match time must not let new orders go live before now
        PaperBot._requote(self, m, max(now, self.clock() * 1000.0), force)

    def _side_quotes(self, m, side, fair, now_s, imb):
        if now_s * 1000.0 < _gs(m)["pull"][side]:
            return []
        return super()._side_quotes(m, side, fair, now_s, imb)

    def _build_snapshot(self):
        PaperBot._build_snapshot(self)
        s = self._snapshot
        now = s["ts"]
        s["signals"] = list(self.signals)[-150:]
        s["spike"] = None if self.spike is None else {"id": self.spike["id"], "move": self.spike["move"] * 1e4}
        s["exchanges"] = {k: (h[-1][1] if h else None, (now * 1000 - h[-1][0]) if h else None)
                          for k, h in self.ex.items()}
        s["pulled"] = {m["slug"]: [sd for sd in ("up", "down") if now * 1000 < _gs(self.markets[m["slug"]])["pull"][sd]]
                       for m in s["markets"] if m["slug"] in self.markets}

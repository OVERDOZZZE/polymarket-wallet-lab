"""Reference implementation of the quoting logic of the final bot ("imbalance maker"), free of any simulator code.
pmre/backtest.py drives these same objects for the imbalance strategy, so what is backtested is what is specified.
A live bot feeds it its book, Binance and fill events and turns the desired order set into post-only orders.

Units: prices in Up terms as integer thousandths (px = price x 1000; 1 c = 10). An Up bid at px is a BUY of the Up
token at px / 1000; a Down bid is a BUY of the Down token at q, written in Up terms as an Up ask at 1000 - 1000 q
(side -1). bb / ba = the exchange's best Up bid / best Up ask (ba = 1 - best Down bid). Sizes in shares.

The bot only ever BUYS (both tokens, resting post-only), holds to settlement (or merges pairs), never takes.

    q = Quoter(QuoterConfig(), window_start_ms)
    q.on_binance(recv_ms, price)                   -> side to pull now (1 Up bids, -1 Down bids) or 0
    q.desired(t_ms, bb, ba, size_bb, size_ba, t_bb, t_ba, net)   -> {(side, px): size}
"""
import math
from collections import deque
from dataclasses import dataclass

TICK = 10


@dataclass
class QuoterConfig:
    B: float = 60.0              # size rule base: size = floor(B / (1 - |p - 0.5|))  (Crouch's rule, B = 60 since 10-21)
    n_levels: int = 13           # ladder depth per side, 1 c apart (Crouch: ~13)
    floor_px: int = 100          # never bid a token below 0.10
    inv_cap: float = 300.0       # stop bidding a token when net shares toward it exceed this in the window
    start_s: float = 5.0         # first quote this long after the window opens
    stop_s: float = 25.0         # cancel everything this long before the window ends (5m window = 300 s)
    window_s: float = 300.0
    imb_join: float = 0.4        # join a token's best bid when its queue share >= imb_join ...
    age_join: float = 0.0        # ... and its best price is at least this old (s)
    back_ticks: int = 2          # else top this many ticks behind its best bid
    imb_low: float = 0.0         # ... or back_low ticks behind when the share < imb_low (0 = off)
    back_low: int = 2
    guard_bps: float = 0.5       # Binance futures move that pulls the threatened side
    guard_win_ms: float = 1000.0  # ... measured over this window (receipt time)
    guard_cool_ms: float = 1000.0  # ... and keeps it off this long after the last triggering tick


def rule_size(B, px):
    """Crouch's size rule for a bid at px (Up-terms px of either side: |p - 0.5| is the same)."""
    return float(math.floor(B / (1 - abs(px / 1000 - 0.5)) + 1e-9))


class BinanceGuard:
    """Move of the last price over the last win_ms (by receipt time): a fall of >= bps threatens the Up bids (takers
    sell Up into them), a rise the Down bids. `until[side]` = receipt time after which the side may quote again."""

    def __init__(self, bps, win_ms, cool_ms):
        self.bps, self.win_ms, self.cool_ms = bps, win_ms, cool_ms
        self.ticks = deque()            # (t, price), kept while t >= now - win_ms - and the one just before
        self.until = {1: -math.inf, -1: -math.inf}

    def on_tick(self, t, price):
        self.ticks.append((t, price))
        while len(self.ticks) >= 2 and self.ticks[1][0] <= t - self.win_ms:
            self.ticks.popleft()
        t0, p0 = self.ticks[0]
        if t0 > t - self.win_ms or self.bps <= 0:
            return 0                    # no tick old enough yet
        r = (price / p0 - 1) * 1e4
        if abs(r) < self.bps:
            return 0
        side = 1 if r < 0 else -1
        self.until[side] = max(self.until[side], t + self.cool_ms)
        return side

    def allowed(self, t):
        return t >= self.until[1], t >= self.until[-1]


class Quoter:
    def __init__(self, cfg: QuoterConfig, window_start_ms: float):
        self.cfg = cfg
        self.t_start = window_start_ms + cfg.start_s * 1000
        self.t_stop = window_start_ms + (cfg.window_s - cfg.stop_s) * 1000
        self.guard = BinanceGuard(cfg.guard_bps, cfg.guard_win_ms, cfg.guard_cool_ms)

    def on_binance(self, recv_ms, price):
        """Feed every Binance futures trade price as received. Returns the side whose orders must be cancelled now
        (and stay off for guard_cool_ms), or 0."""
        return self.guard.on_tick(recv_ms, price)

    def top_depths(self, size_bb, size_ba, age_bb_s, age_ba_s):
        """Ticks behind the token's best bid for the Up and the Down ladder top, from the queue share at the best:
        Up share = size at the best Up bid / (that + size at the best Up ask); Down share = 1 - Up share."""
        c = self.cfg
        tot = size_bb + size_ba
        sh_up = size_bb / tot if tot > 0 else 0.5
        out = []
        for sh, age in ((sh_up, age_bb_s), (1 - sh_up, age_ba_s)):
            if sh >= c.imb_join and age >= c.age_join:
                out.append(0)
            elif c.imb_low > 0 and sh < c.imb_low:
                out.append(c.back_low)
            else:
                out.append(c.back_ticks)
        return tuple(out)

    def desired(self, t, bb, ba, size_bb, size_ba, t_bb, t_ba, net, allow=(True, True)):
        """Desired resting orders {(side, px): size} at exchange time t (ms). size_bb / size_ba: others' resting size
        at the best bid / ask (Up terms; our own orders excluded); t_bb / t_ba: when the best bid / ask price was set;
        net: Up shares bought - Down shares bought in this window; allow: extra per-side switch."""
        c = self.cfg
        if t < self.t_start or t >= self.t_stop or bb <= 0 or ba <= 0:
            return {}
        g = self.guard.allowed(t)
        d_up, d_dn = self.top_depths(size_bb, size_ba, (t - t_bb) / 1000, (t - t_ba) / 1000)
        want = {}
        if net <= c.inv_cap and g[0] and allow[0]:
            top = bb - d_up * TICK
            for k in range(c.n_levels):
                px = top - k * TICK
                if px < c.floor_px:
                    break
                want[(1, px)] = rule_size(c.B, px)
        if net >= -c.inv_cap and g[1] and allow[1]:
            top = ba + d_dn * TICK
            for k in range(c.n_levels):
                px = top + k * TICK
                if px > 1000 - c.floor_px:
                    break
                want[(-1, px)] = rule_size(c.B, px)
        return want

"""Imbalance maker (strategy "imb_maker"): CrouchPredictions' ladder with a queue-share gate on the top and a
Binance futures guard. Built 2026-10-25 from the user's study, docs/bots/bot_sim/BOT_SPEC.md (sections 3-5); the
decision code is a port of docs/bots/bot_sim/strategy.py (rule_size, BinanceGuard, Quoter.top_depths,
Quoter.desired), checked against it by scripts/check_imb_maker.py. Paper only (phase 1 of the spec).

Rules, per BTC 5m window (prices in Up terms as integer thousandths, px = price x 1000, 1 c = 10):
  * quote from start + start_delay_s (5) to end - stop_before_end_s (25); cancel everything outside;
  * bb = best Up bid, ba = 1 - best Down bid; share_up = size at bb / (size at bb + size at the best Down bid),
    share_down = 1 - share_up (0.5 if both are 0);
  * each ladder's top joins its token's best bid when its share >= imb_join (0.4), else sits back_ticks (2)
    behind it; n_levels (13) bids 1 c apart going away from the market, none below min_price (0.10) in the
    token's own price, size floor(B / (1 - |p - 0.5|)) (B = 60);
  * no Up bids while net (Up shares - Down shares bought in the window) > inv_cap (300), no Down bids while < -300;
  * guard: on every Binance USD-M futures trade, r = price / (last price received at or before now - lookback_ms)
    - 1. r <= -spike_bps pulls every Up bid at once and keeps Up out until pull_ms after the last such trade;
    r >= +spike_bps the same for Down;
  * on every book message, fill, guard trigger and the engine's 50 ms tick: the desired set is recomputed;
    resting orders at prices no longer wanted are cancelled, missing ones placed, wanted ones left alone.
Buys only, post-only, held to settlement; no fair-value filter, no takes, no skew.

Where the app's engine is used as is: queue-aware paper fills (market_maker.py, cancels ahead of us pro rata),
order / cancel latency (latency_ms / cancel_latency_ms, 35 ms measured from Dublin), prints judged at their match
time (guard_maker), settlement and persistence. Where this module replaces the inherited behaviour, and why:
  * the guard is the spec's BinanceGuard on every trade (FastBot's spike detector skips repeated prices, so later
    trades at the new price would not extend the cooldown);
  * quoting does not wait for the price model (PaperBot's requote needs a strike first); the model is computed
    for the UI only;
  * one addition the spec does not have: while a guard feed has sent nothing for feed_stale_ms (10 s: a dead feed,
    not a quiet market) or has not started, nothing is quoted, because the unguarded ladder is the losing version
    of this strategy.
  * rebate_share = 0.20: the spec's section 6 found 0.2 x fee rate x p(1-p) exact (the engine default is 0.15).
Inherited settings this strategy ignores: levels, sizes, base_edge, level_step, model_weight, lead_weight, join_only,
skew_per_100, max_side_shares, max_imbalance, max_window_cost, max_price, jump_*, taker_*, combine.

Logs (for comparing paper, live and the study's simulator): one file per window,
data/bots/<id>/windows/<UTC day>/<slug>.json.gz, written when the window settles (kept LOG_KEEP_DAYS). Every order
(side, price, size, sent, live, cancel sent, cancel effective, filled, queue ahead at placement, cancel reason),
every fill (time, order, price, shares), every guard trigger (source, price, reference price, move, side, pulled
until, orders pulled) and every decision whose inputs changed (bb, ba, sizes at both, share, depths, net, what each
side did). Times are this machine's epoch ms (add clock_offset_ms for true time).
"""
import dataclasses
import gzip
import math
import os
import queue
import shutil
import threading
import time
from collections import deque

import orjson

from ...util import replace_retry
from .guard_maker import GuardMakerBot, GuardMakerConfig
from .market_maker import PaperBot

TICK = 10                 # 1 c in thousandths
LOG_KEEP_DAYS = 30
ORDER_COLS = ("id", "side", "price", "size", "sent_ms", "live_ms", "cancel_ms", "cancel_eff_ms", "filled",
              "queue0", "why")
FILL_COLS = ("ts_ms", "order", "side", "price", "shares")
GUARD_COLS = ("ts_ms", "src", "price", "ref", "bps", "side", "until_ms", "pulled")
# per side: q quoting, g guard, c inventory cap, s guard feed stale, b no book
DEC_COLS = ("ts_ms", "bb", "ba", "size_bb", "size_ba", "share_up", "d_up", "d_dn", "net", "up", "dn")


@dataclasses.dataclass
class ImbMakerConfig(GuardMakerConfig):
    bankroll: float = 5000.0             # covers the worst window + resting ladders + unredeemed positions (spec 6)
    windows: tuple = (5,)
    # quoting (spec 3-4)
    B: float = 60.0                      # size = floor(B / (1 - |p - 0.5|))
    n_levels: int = 13
    inv_cap: float = 300.0
    imb_join: float = 0.4                # join the best bid when the token's queue share is at least this ...
    age_join: float = 0.0                # ... and its best price is at least this old (s)
    back_ticks: int = 2                  # else the top sits this many ticks behind the best bid
    imb_low: float = 0.0                 # optional third tier: back_low ticks when the share < imb_low (0 = off)
    back_low: int = 2
    min_price: float = 0.10              # no bid below this in the token's own price
    max_price: float = 0.99
    start_delay_s: float = 5.0
    stop_before_end_s: float = 25.0
    maker_min_edge: float = -1.0         # no model filter
    jump_bps: float = 1e9                # no Coinbase jump pull
    # guard: Binance USD-M futures last trade
    sources: tuple = ("binance_fut",)
    spike_bps: float = 0.5
    lookback_ms: float = 1000.0
    pull_ms: float = 1000.0
    feed_stale_ms: float = 10000.0       # no quotes once the guard feed is this silent (quiet Sundays: gaps > 3 s)
    # simulation (Dublin)
    latency_ms: float = 35.0
    cancel_latency_ms: float = 35.0
    min_requote_ms: float = 0.0
    rebate_share: float = 0.20


# ---------------------------------------------------------------- decision code (port of strategy.py)
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


def top_depths(c, size_bb, size_ba, age_bb_s, age_ba_s):
    """Ticks behind the token's best bid for the Up and the Down ladder top, from the queue share at the best."""
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


def ladders(c, bb, ba, d_up, d_dn, net, allow=(True, True)):
    """Quoter.desired past its clock check: {(side, px): size}, side 1 = Up bid at px, -1 = Down bid at 1000 - px."""
    floor_px = round(c.min_price * 1000)
    want = {}
    if net <= c.inv_cap and allow[0]:
        top = bb - d_up * TICK
        for k in range(c.n_levels):
            px = top - k * TICK
            if px < floor_px:
                break
            want[(1, px)] = rule_size(c.B, px)
    if net >= -c.inv_cap and allow[1]:
        top = ba + d_dn * TICK
        for k in range(c.n_levels):
            px = top + k * TICK
            if px > 1000 - floor_px:
                break
            want[(-1, px)] = rule_size(c.B, px)
    return want


# ---------------------------------------------------------------- engine
def _ist(m):
    """Per-window state and logs, kept on the Market."""
    st = m.__dict__.get("im")
    if st is None:
        st = m.im = {"orders": {}, "fills": [], "guard": [], "dec": [], "last": None, "bb": (None, 0.0),
                     "ba": (None, 0.0), "fair_ms": 0.0}
    return st


class ImbMakerBot(GuardMakerBot):
    CONFIG = ImbMakerConfig
    STRATEGY = "imb_maker"

    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self.guards = {}                              # source -> BinanceGuard
        self._episode = None                          # the guard move in progress (UI rows, one per window)
        self.log_dir = None if self.offline else self.data_dir / "windows"
        self._logq = None

    # ------------------------------------------------------------------ guard
    def _on_ex(self, src, price, recv):
        h = self.ex[src]
        h.clear()                                     # the UI needs the last tick only; the guard keeps its own
        h.append((recv, price))
        if src not in self.cfg.sources:
            return
        cfg = self.cfg
        g = self.guards.get(src)
        if g is None:
            g = self.guards[src] = BinanceGuard(cfg.spike_bps, cfg.lookback_ms, cfg.pull_ms)
        g.bps, g.win_ms, g.cool_ms = cfg.spike_bps, cfg.lookback_ms, cfg.pull_ms     # settings apply live
        side = g.on_tick(recv, price)
        if side:
            self._on_guard(src, g, side, price, recv)

    def _on_guard(self, src, g, side, price, now):
        """A trigger: every bid on the threatened token is cancelled now; the other side keeps quoting."""
        tok = "up" if side == 1 else "down"
        ref = g.ticks[0][1]
        mv = price / ref - 1.0
        self.counts["guard_triggers"] += 1
        ep = self._episode
        if ep is None or ep["side"] != tok or now > ep["until"]:
            self._spike_n += 1
            ep = self._episode = {"id": self._spike_n, "side": tok, "seen": set()}
            self.counts["guard_moves"] += 1
        ep["move"], ep["until"] = mv, g.until[side]
        now_s = now / 1000.0
        for m in list(self.markets.values()):
            if m.state not in ("pending", "quoting") or not m.start <= now_s < m.end:
                continue
            st = _ist(m)
            pulled = 0
            for o in m.orders:
                if o.side == tok and not o.taker and o.cancel_ms is None:
                    o.cancel_ms = now
                    e = st["orders"].get(o.id)
                    if e is not None:
                        e[2] = "guard"
                    pulled += 1
            self.counts["guard_cancels"] += pulled
            st["guard"].append((round(now, 1), src, price, ref, round(mv * 1e4, 3), tok, round(g.until[side], 1),
                                pulled))
            first = m.slug not in ep["seen"]
            ep["seen"].add(m.slug)
            if first or pulled:
                self.signals.append({"ts": now_s, "slug": m.slug, "win": m.minutes, "t_left": round(m.end - now_s, 1),
                                     "side": tok, "move": round(mv * 1e4, 2), "spike": ep["id"], "ask": None,
                                     "fair": None, "edge": None, "limit": None, "usd": None,
                                     "result": "pulled" if pulled else "none_resting", "filled": pulled or None,
                                     "avg": None})
            self._requote(m, now, force=True)

    def _guard_allow(self, now):
        """(Up allowed, Down allowed, stale): every guard source must be live and quiet for a side to quote."""
        up = dn = True
        for src in self.cfg.sources:
            g = self.guards.get(src)
            if g is None or not g.ticks or now - g.ticks[-1][0] > self.cfg.feed_stale_ms:
                return False, False, True
            a = g.allowed(now)
            up, dn = up and a[0], dn and a[1]
        return up, dn, False

    # ------------------------------------------------------------------ quoting
    def _requote(self, m, now, force=False):
        now = max(now, self.clock() * 1000.0)        # a fill judged at its match time must not place orders in the past
        cfg = self.cfg
        if not force and now - m.last_quote_ms < cfg.min_requote_ms:
            return
        m.last_quote_ms = now
        now_s = now / 1000.0
        if now_s >= m.end:
            self._pull_all(m, now, "end")
            if m.state in ("pending", "quoting"):
                m.state = "closed"
            return
        st = _ist(m)
        if now - st["fair_ms"] >= 250:               # the price model is for the UI only
            st["fair_ms"] = now
            m.fair = self._model(m, now_s)
        if not m.start + cfg.start_delay_s <= now_s < m.end - cfg.stop_before_end_s:
            self._pull_all(m, now, "clock")
            return
        m.state = "quoting"
        want, why = self._desired(m, now, st)
        for side in ("up", "down"):
            m.quotes[side] = want[side]
            self._reconcile(m, side, want[side], now)
            for o in m.orders:
                if o.side == side and o.cancel_ms == now:
                    e = st["orders"].get(o.id)
                    if e is not None and e[2] is None:
                        e[2] = why[side]

    def _desired(self, m, now, st):
        """The spec's decision for this moment: ({side: [(price, size)]}, {side: reason a cancel is logged with});
        logs the inputs whenever they changed."""
        cfg = self.cfg
        bb_p, qd_p = m.best_bid("up"), m.best_bid("down")
        if not m.book_ok or bb_p is None or qd_p is None:
            self._log_decision(st, (round(now, 1), None, None, None, None, None, None, None,
                                    round(m.shares["up"] - m.shares["down"], 2), "b", "b"))
            return {"up": [], "down": []}, {"up": "no_book", "down": "no_book"}
        bb, ba = round(bb_p * 1000), 1000 - round(qd_p * 1000)
        size_bb, size_ba = m.books["up"]["bids"][bb_p], m.books["down"]["bids"][qd_p]   # paper: ours are not in it
        if st["bb"][0] != bb:
            st["bb"] = (bb, now)
        if st["ba"][0] != ba:
            st["ba"] = (ba, now)
        net = m.shares["up"] - m.shares["down"]
        g_up, g_dn, stale = self._guard_allow(now)
        d_up, d_dn = top_depths(cfg, size_bb, size_ba, (now - st["bb"][1]) / 1000.0, (now - st["ba"][1]) / 1000.0)
        want = ladders(cfg, bb, ba, d_up, d_dn, net, (g_up, g_dn))
        out = {"up": [(px / 1000, sz) for (s, px), sz in want.items() if s == 1],
               "down": [((1000 - px) / 1000, sz) for (s, px), sz in want.items() if s == -1]}
        why = {"up": "stale" if stale else "guard" if not g_up else "cap" if net > cfg.inv_cap else "requote",
               "down": "stale" if stale else "guard" if not g_dn else "cap" if net < -cfg.inv_cap else "requote"}
        tot = size_bb + size_ba
        code = {"requote": "q", "guard": "g", "cap": "c", "stale": "s"}
        self._log_decision(st, (round(now, 1), bb, ba, size_bb, size_ba, round(size_bb / tot if tot > 0 else 0.5, 4),
                                d_up, d_dn, round(net, 2), code[why["up"]], code[why["down"]]))
        return out, why

    def _log_decision(self, st, row):
        if st["last"] == row[1:]:
            return
        if "s" in row[9:] and (st["last"] is None or "s" not in st["last"][8:]):
            self.counts["guard_stale"] += 1
        st["last"] = row[1:]
        st["dec"].append(row)

    def _pull_all(self, m, now, why):
        m.quotes = {"up": [], "down": []}
        if not m.orders:
            return
        reg = _ist(m)["orders"]
        for o in m.orders:
            if o.cancel_ms is None and not o.taker:
                e = reg.get(o.id)
                if e is not None:
                    e[2] = why
        self._cancel_all(m, now)

    def _reconcile(self, m, side, want, now):
        n0 = self._oid
        PaperBot._reconcile(self, m, side, want, now)
        if self._oid != n0:                           # new orders are appended at the end, in id order
            reg = _ist(m)["orders"]
            for o in reversed(m.orders):
                if o.id <= n0:
                    break
                reg[o.id] = [o, o.queue, None]

    def _fill(self, m, o, shares, price, now, maker):
        _ist(m)["fills"].append((round(now, 1), o.id, o.side, price, shares))
        super()._fill(m, o, shares, price, now, maker)

    # ------------------------------------------------------------------ window logs
    def _provisional_settle(self, m):
        super()._provisional_settle(m)
        self._write_log(m)

    def _write_log(self, m):
        st = m.__dict__.pop("im", None)
        if self.log_dir is None or st is None:
            return
        clat = self._cancel_lat()
        orders = [(o.id, o.side, o.price, o.size, round(o.placed_ms, 1), round(o.active_ms, 1),
                   None if o.cancel_ms is None else round(o.cancel_ms, 1),
                   None if o.cancel_ms is None else round(o.cancel_ms + clat, 1),
                   round(o.size - o.remaining, 6), q0, why) for o, q0, why in st["orders"].values()]
        rec = {"bot": self.bot_id, "account": self.account, "strategy": self.STRATEGY, "slug": m.slug,
               "start": m.start, "end": m.end, "cfg": self.cfg.to_dict(), "clock_offset_ms": self.clock_offset_ms,
               "strike": m.strike, "strike_src": m.strike_src, "winner": m.winner, "winner_src": m.winner_src,
               "shares": dict(m.shares), "cost": dict(m.cost), "rebate_est": m.fee_equiv * self.cfg.rebate_share,
               "orders": {"cols": ORDER_COLS, "rows": orders},
               "fills": {"cols": FILL_COLS, "rows": st["fills"]},
               "guard": {"cols": GUARD_COLS, "rows": st["guard"]},
               "decisions": {"cols": DEC_COLS, "rows": st["dec"]}}
        path = self.log_dir / time.strftime("%Y-%m-%d", time.gmtime(m.start)) / f"{m.slug}.json.gz"
        if self._logq is None:
            self._logq = queue.Queue()
            threading.Thread(target=self._log_writer, daemon=True, name="window-logs").start()
        self._logq.put((path, rec))

    def _log_writer(self):
        """Off the engine thread (gzip releases the GIL): one file per window, old days pruned."""
        day = None
        while True:
            path, rec = self._logq.get()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_bytes(gzip.compress(orjson.dumps(rec), compresslevel=6))
                replace_retry(tmp, path)
                if path.parent.name != day:
                    day = path.parent.name
                    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - LOG_KEEP_DAYS * 86400))
                    for d in path.parent.parent.iterdir():
                        if d.is_dir() and d.name < cutoff:
                            shutil.rmtree(d, ignore_errors=True)
            except Exception as e:
                self._err(f"window log: {type(e).__name__}: {e}"[:200])

    # ------------------------------------------------------------------ UI
    def _build_snapshot(self):
        PaperBot._build_snapshot(self)
        s = self._snapshot
        now = s["ts"]
        g_up, g_dn, stale = self._guard_allow(now * 1000.0)
        ep = self._episode
        s["signals"] = list(self.signals)[-150:]
        s["spike"] = ({"id": ep["id"], "move": ep["move"] * 1e4} if ep and now * 1000 < ep["until"] else None)
        s["exchanges"] = {k: (h[-1][1] if h else None, (now * 1000 - h[-1][0]) if h else None)
                          for k, h in self.ex.items()}
        s["guard"] = {"up_ok": g_up, "down_ok": g_dn, "stale": stale}
        s["pulled"] = {m["slug"]: [sd for sd, ok in (("up", g_up), ("down", g_dn)) if not ok] for m in s["markets"]}
        s["decisions"] = {}
        for m in self.markets.values():
            st = m.__dict__.get("im")
            if st and st["dec"]:
                s["decisions"][m.slug] = dict(zip(DEC_COLS, st["dec"][-1]))

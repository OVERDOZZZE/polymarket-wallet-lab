"""Imbalance maker (strategy "imb_maker"): CrouchPredictions' ladder with a queue-share gate on the top and a
Binance futures guard. Built 2026-10-25 from the user's study, docs/bots/bot_sim/BOT_SPEC.md (sections 3-5); the
decision code is a port of docs/bots/bot_sim/strategy.py (rule_size, BinanceGuard, Quoter.top_depths,
Quoter.desired), checked against it by scripts/check_imb_maker.py.

Rules, per BTC 5m window (prices in Up terms as integer thousandths, px = price x 1000, 1 c = 10):
  * quote from start + start_delay_s (5) to end - stop_before_end_s (25); cancel everything outside;
  * bb = best Up bid, ba = 1 - best Down bid; share_up = size at bb / (size at bb + size at the best Down bid),
    share_down = 1 - share_up (0.5 if both are 0); our own live orders are left out of the book first;
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
  * spend_cap_usd (0 = off): a window whose fills cost this much is not quoted any more (spec phase 2: ~$100).
Inherited settings this strategy ignores: levels, sizes, base_edge, level_step, model_weight, lead_weight, join_only,
skew_per_100, max_side_shares, max_imbalance, max_window_cost, max_price, jump_*, taker_*, combine.

Live (bots.mode = 'live', read at start; BOT_SPEC.md phase 2). The paper engine keeps running as the twin; the
same decision (same book, guard and clock, but the live window's own inventory and spend) drives real post-only
GTC BUY orders through the bot's own Gateway: batches of up to 15 per request, batch cancels, no refreshes per
order. Fills come from the CLOB user channel (maker_orders of trade events, and size_matched of order events),
matched to our order ids, so another bot on the same wallet never mixes in. Safety:
  * live_paused (kill switch) cancels every live order at once and places none;
  * live_loss_limit_usd: no quoting once settled live trading PnL + the worst case of open windows (the worse side
    wins) reaches minus this; resting orders still filling in flight can take it a little past;
  * on stop (UI, SIGTERM) every live order is cancelled; at start, orders left by an earlier run are cancelled;
    1 s after a window stops quoting, every order of the account still open in that market is cancelled (a post
    that failed in transit may have landed; manual orders in that market go too);
  * rejections back off: post-only-would-cross 0.3 s at that price, no balance / errors 2 s for every window; a
    failed cancel request is retried after 0.2 s. Polymarket's per-signer limits (standard tier: 40 orders/s,
    burst 60; 80 cancels/s, burst 120; a batch costs one token per order) leave room: this sends ~10 of each a
    second while quoting, in batches of at most 15.
With live_merge, each window's balanced Up + Down pairs are merged back into USDC 5 s after it closes (winners are
redeemed by Polymarket). live_dry_run (paper mode only) runs the whole live order flow with simulated 30 ms acks
and sends nothing, to check it on real books. The live ledger is rebuilt from live_orders (origin = bot id) on a
restart.

Logs (for comparing paper, live and the study's simulator): one file per window,
data/bots/<id>/windows/<UTC day>/<slug>.json.gz, written when the window settles (kept LOG_KEEP_DAYS). Every order
(side, price, size, sent, live, cancel sent, cancel effective, filled, queue ahead at placement, cancel reason),
every fill (time, order, price, shares), every guard trigger (source, price, reference price, move, side, pulled
until, orders pulled) and every decision whose inputs changed (bb, ba, sizes at both, share, depths, net, what each
side did); live: every live order (sent, exchange ack, cancel sent / acked, matched, reason, rejection) and fill.
Times are this machine's epoch ms (add clock_offset_ms for true time).
"""
import asyncio
import collections
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

from ... import db
from ...util import replace_retry
from .guard_maker import GuardMakerBot, GuardMakerConfig
from .market_maker import PaperBot, _pct

TICK = 10                 # 1 c in thousandths
LOG_KEEP_DAYS = 30
BATCH = 15                # orders per POST /orders (Polymarket's limit)
ORDER_COLS = ("id", "side", "price", "size", "sent_ms", "live_ms", "cancel_ms", "cancel_eff_ms", "filled",
              "queue0", "why")
FILL_COLS = ("ts_ms", "order", "side", "price", "shares")
GUARD_COLS = ("ts_ms", "src", "price", "ref", "bps", "side", "until_ms", "pulled")
# per side: q quoting, g guard, c inventory cap, s guard feed stale, b no book, x spend cap
DEC_COLS = ("ts_ms", "bb", "ba", "size_bb", "size_ba", "share_up", "d_up", "d_dn", "net", "up", "dn")
LIVE_ORDER_COLS = ("lid", "oid", "side", "price", "size", "sent_ms", "ack_ms", "post_ms", "status", "cancel_ms",
                   "cancel_ack_ms", "matched", "why", "code", "error")
LIVE_FILL_COLS = ("ts_ms", "oid", "side", "price", "shares", "trade")


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
    spend_cap_usd: float = 0.0           # a window whose fills cost this much is not quoted any more (0 = off)
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
    # live (bots.mode = 'live', read at start)
    live_paused: bool = False            # kill switch: every live order cancelled at once, none placed
    live_loss_limit_usd: float = 60.0    # no quoting once settled live PnL + worst case of open windows <= -this
    live_merge: bool = True              # merge each window's Up + Down pairs into USDC after it closes
    live_dry_run: bool = False           # paper mode: run the live order flow with simulated acks, send nothing


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


def _by_token(want):
    """{(side, px): size} -> {"up": {price: size}, "down": {price: size}} in each token's own price, ladder order."""
    return {"up": {round(px / 1000, 4): sz for (s, px), sz in want.items() if s == 1},
            "down": {round((1000 - px) / 1000, 4): sz for (s, px), sz in want.items() if s == -1}}


# ---------------------------------------------------------------- engine
def _ist(m):
    """Per-window state and logs, kept on the Market."""
    st = m.__dict__.get("im")
    if st is None:
        st = m.im = {"orders": {}, "fills": [], "guard": [], "dec": [], "last": None, "bb": (None, 0.0),
                     "ba": (None, 0.0), "fair_ms": 0.0, "inp": None, "lfills": []}
    return st


def _lv(m):
    """Per-window live order state, kept on the Market."""
    lv = m.__dict__.get("lv")
    if lv is None:
        lv = m.lv = {"lids": [], "act": [], "backoff": {}}      # every order sent / those not finished yet
    return lv


ACTIVE = ("sending", "live", "cancelling")


class ImbMakerBot(GuardMakerBot):
    CONFIG = ImbMakerConfig
    STRATEGY = "imb_maker"

    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self.guards = {}                              # source -> BinanceGuard
        self._episode = None                          # the guard move in progress (UI rows, one per window)
        self.log_dir = None if self.offline else self.data_dir / "windows"
        self._logq = None
        # live (GuardMakerBot forces paper; this strategy has its own live path)
        if not self.offline:
            self.mode = (db.one("SELECT mode FROM bots WHERE id = ?", (bot_id,)) or {}).get("mode") or "paper"
        self.dry = self.mode != "live" and bool(self.cfg.live_dry_run) and not self.offline
        self.live_on = self.mode == "live" or self.dry
        self.lorders, self.loid = {}, {}              # local id -> live order, exchange id -> live order
        self._lid = 0
        self._ltrades = set()                         # (trade id, order id) counted
        self._lblock = 0.0                            # no live placements before this (ms): balance / errors
        self._lcretry = 0.0                           # a failed cancel request is retried from this time (ms)
        self._lready = False                          # gateway up and leftovers cancelled
        self._lrisk = (0.0, 0.0)                      # (settled live PnL, worst case of open windows)
        self._lprune = 0.0
        self.lstat = collections.Counter()
        self.lpost_ms, self.lcancel_ms = deque(maxlen=2000), deque(maxlen=2000)
        if self.mode == "live":
            self._load_live_maker()

    def _load_live(self):
        """FastBot's taker ledger does not apply here (see _load_live_maker)."""

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
            self._requote(m, now, force=True)         # the live side is cancelled here too

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
            self._live_sync(m, now, "end")
            if m.state in ("pending", "quoting"):
                m.state = "closed"
            return
        st = _ist(m)
        if now - st["fair_ms"] >= 250:               # the price model is for the UI only
            st["fair_ms"] = now
            m.fair = self._model(m, now_s)
        if not m.start + cfg.start_delay_s <= now_s < m.end - cfg.stop_before_end_s:
            self._pull_all(m, now, "clock")
            self._live_sync(m, now, "clock")
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
        self._live_sync(m, now)

    def _best_excl(self, m, side):
        """(best bid, size there) of one token without our own live orders in it (paper: the book as it is)."""
        bids = m.books[side]["bids"]
        if not bids:
            return None, 0.0
        mine = self._mine(m, side)
        if not mine:
            px = max(bids)
            return px, bids[px]
        for px in sorted(bids, reverse=True):
            sz = bids[px] - mine.get(round(px, 4), 0.0)
            if sz > 1e-6:
                return px, sz
        return None, 0.0

    def _mine(self, m, side):
        """{price: shares} of our live orders resting in the book on one token."""
        lv = m.__dict__.get("lv")
        if not lv:
            return {}
        out = {}
        for lid in lv["act"]:
            o = self.lorders.get(lid)
            if o is not None and o["side"] == side and o["status"] in ("live", "cancelling"):
                k = round(o["price"], 4)
                out[k] = out.get(k, 0.0) + max(0.0, o["size"] - o["matched"])
        return out

    def _desired(self, m, now, st):
        """The spec's decision for this moment: ({side: [(price, size)]}, {side: reason a cancel is logged with});
        logs the inputs whenever they changed. The inputs are kept for the live layer (st["inp"])."""
        cfg = self.cfg
        bb_p, size_bb = self._best_excl(m, "up")
        qd_p, size_ba = self._best_excl(m, "down")
        net = m.shares["up"] - m.shares["down"]
        if not m.book_ok or bb_p is None or qd_p is None:
            st["inp"] = None
            self._log_decision(st, (round(now, 1), None, None, None, None, None, None, None, round(net, 2), "b", "b"))
            return {"up": [], "down": []}, {"up": "no_book", "down": "no_book"}
        bb, ba = round(bb_p * 1000), 1000 - round(qd_p * 1000)
        if st["bb"][0] != bb:
            st["bb"] = (bb, now)
        if st["ba"][0] != ba:
            st["ba"] = (ba, now)
        g_up, g_dn, stale = self._guard_allow(now)
        d_up, d_dn = top_depths(cfg, size_bb, size_ba, (now - st["bb"][1]) / 1000.0, (now - st["ba"][1]) / 1000.0)
        st["inp"] = (bb, ba, d_up, d_dn, g_up, g_dn, stale)
        spent = cfg.spend_cap_usd > 0 and m.total_cost() >= cfg.spend_cap_usd
        want = {} if spent else ladders(cfg, bb, ba, d_up, d_dn, net, (g_up, g_dn))
        out = {k: list(v.items()) for k, v in _by_token(want).items()}
        why = {s: self._why(stale, g, spent, capped) for s, g, capped in
               (("up", g_up, net > cfg.inv_cap), ("down", g_dn, net < -cfg.inv_cap))}
        tot = size_bb + size_ba
        code = {"requote": "q", "guard": "g", "cap": "c", "stale": "s", "spend_cap": "x"}
        self._log_decision(st, (round(now, 1), bb, ba, size_bb, size_ba, round(size_bb / tot if tot > 0 else 0.5, 4),
                                d_up, d_dn, round(net, 2), code[why["up"]], code[why["down"]]))
        return out, why

    @staticmethod
    def _why(stale, allowed, spent, capped):
        return "stale" if stale else "guard" if not allowed else "spend_cap" if spent else "cap" if capped else "requote"

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

    def _tick(self, now):
        super()._tick(now)
        if not self.live_on:
            return
        self._lrisk = self._live_risk()
        for m in list(self.markets.values()):
            lv = m.__dict__.get("lv")                  # 1 s after quoting stops: cancel anything still open in this
            if (lv and lv["lids"] and not lv.get("swept") and self.gw is not None and m.condition   # market (a post that
                    and now / 1000.0 >= m.end - self.cfg.stop_before_end_s + 1):                   # failed in transit
                lv["swept"] = True                                                                  # may have landed)
                self._live_sweep(m)
            w = self.live.get(m.slug)                  # merge the pairs of a closed window
            if (w is not None and w.get("merge") is None and self.gw is not None and self.cfg.live_merge
                    and now / 1000.0 >= m.end + 5 and min(w["up_sh"], w["down_sh"]) >= 1.0 and m.condition):
                self._live_merge(m, w)
        if now - self._lprune > 10_000:
            self._lprune = now
            old = [lid for lid, o in self.lorders.items() if now / 1000.0 > o["end"] + 600]
            for lid in old:
                o = self.lorders.pop(lid)
                self.loid.pop(o["oid"], None)

    # ------------------------------------------------------------------ live: orders
    def _live_sync(self, m, now, off=None):
        """Bring this window's real orders to the live decision: cancel what is not wanted, place what is missing.
        Same inputs as the paper decision, but the live window's own inventory and spend."""
        if not self.live_on:
            return
        lv = _lv(m)
        lv["act"] = [lid for lid in lv["act"] if lid in self.lorders and self.lorders[lid]["status"] in ACTIVE]
        if not lv["act"] and (off or m.state != "quoting"):
            return
        cfg = self.cfg
        w = self.live.get(m.slug)
        net = (w["up_sh"] - w["down_sh"]) if w else 0.0
        inp = None if off else (m.__dict__.get("im") or {}).get("inp")
        reason = off or self._live_block(w) or (None if inp else "no_book")
        if reason is None:
            bb, ba, d_up, d_dn, g_up, g_dn, stale = inp
            want = _by_token(ladders(cfg, bb, ba, d_up, d_dn, net, (g_up, g_dn)))
            why = {s: self._why(stale, g, False, capped) for s, g, capped in
                   (("up", g_up, net > cfg.inv_cap), ("down", g_dn, net < -cfg.inv_cap))}
        else:
            want, why = {"up": {}, "down": {}}, {"up": reason, "down": reason}
        cancels, places = [], []
        for side in ("up", "down"):
            keep = set()
            for lid in lv["act"]:
                o = self.lorders.get(lid)
                if o is None or o["side"] != side or o["status"] not in ("sending", "live"):
                    continue
                if o["want_cancel"]:                  # unwanted while in flight: cancel once acked
                    if o["status"] == "live":
                        cancels.append((o, o["want_cancel"]))
                    continue
                k = round(o["price"], 4)
                if k in want[side] and k not in keep:
                    keep.add(k)
                else:
                    cancels.append((o, why[side]))
            if now >= self._lblock:
                for px, sz in want[side].items():
                    if px not in keep and now >= lv["backoff"].get((side, px), 0.0):
                        places.append((side, px, sz))
        if cancels:
            self._live_cancel(cancels, now)
        if places:
            self._live_place(m, lv, places, now)

    def _live_block(self, w):
        """Why the live layer may not quote at all right now, or None."""
        cfg = self.cfg
        if not self._lready:
            return "starting"
        if cfg.live_paused:
            return "paused"
        if self.gw is not None and not self.gw.enabled:
            return "no_gateway"
        realized, worst = self._lrisk
        if realized + worst <= -cfg.live_loss_limit_usd + 1e-9:
            return "loss_limit"
        if w is not None and cfg.spend_cap_usd > 0 and w["up_cost"] + w["down_cost"] >= cfg.spend_cap_usd:
            return "spend_cap"
        return None

    def _live_call(self, fn, *args):
        """Run a gateway call on its order pool; dry run: answer after 30 ms without sending anything."""
        if self.gw is not None:
            return self._loop.run_in_executor(self.gw.pool, fn, *args)
        fut = self._loop.create_future()
        self._loop.call_later(0.03, lambda: fut.done() or fut.set_result(fn(*args)))
        return fut

    def _live_place(self, m, lv, places, now):
        for i in range(0, len(places), BATCH):
            items, req = [], []
            for side, px, sz in places[i:i + BATCH]:
                self._lid += 1
                o = {"lid": self._lid, "slug": m.slug, "win": m.minutes, "start": m.start, "end": m.end, "side": side,
                     "price": px, "size": sz, "matched": 0.0, "tsum": 0.0, "sent": now, "ack": None, "post_ms": None,
                     "oid": None, "status": "sending", "csent": None, "cack": None, "why": None, "code": None,
                     "err": None, "want_cancel": None}
                self.lorders[o["lid"]] = o
                lv["lids"].append(o["lid"])
                lv["act"].append(o["lid"])
                items.append(o)
                req.append((m.slug, side, m.tokens[side], px, sz))
            self.lstat["sent"] += len(items)
            self.lstat["post_batches"] += 1
            fn = self.gw.post_limit_batch if self.gw is not None else self._dry_post
            fut = self._live_call(fn, req, self.bot_id)
            fut.add_done_callback(lambda f, items=items, m=m: self._live_placed(f, items, m))

    def _live_placed(self, f, items, m):
        now = self.clock() * 1000.0
        try:
            r = f.result()
        except Exception as e:
            r = {"orders": [{"ok": False, "order_id": None, "error": f"{type(e).__name__}: {e}"[:200],
                             "code": "exception"} for _ in items], "post_ms": None}
        if r.get("post_ms") is not None:
            self.lpost_ms.append(r["post_ms"])
        lv = _lv(m)
        for o, res in zip(items, r["orders"]):
            o["ack"], o["post_ms"] = now, r.get("post_ms")
            if res["ok"]:
                o["oid"], o["status"] = res["order_id"], "live"
                self.loid[o["oid"]] = o
                self.lstat["live"] += 1
                if o["want_cancel"]:                  # unwanted while it was on its way
                    self._live_cancel([(o, o["want_cancel"])], now)
                continue
            o["status"], o["err"], o["code"] = "rejected", res["error"], res["code"]
            self.lstat["rej_" + str(res["code"])] += 1
            if res["code"] == "post_only_would_cross":
                lv["backoff"][(o["side"], o["price"])] = now + 300
            else:
                lv["backoff"][(o["side"], o["price"])] = now + 1000
                if res["code"] in ("not_enough_balance", "exception", "error", "unknown"):
                    self._lblock = now + 2000
                self._live_log("rejected", o, info=res["error"])
                if res["code"] == "exception":
                    self._err(f"live order: {res['error']}")

    def _live_cancel(self, pairs, now):
        ids = []
        for o, why in pairs:
            if o["status"] == "sending":
                o["want_cancel"] = o["want_cancel"] or why     # cancelled once its id is back
                continue
            if o["status"] != "live" or now < self._lcretry:   # last cancel request failed (e.g. 429): retry soon
                continue
            o["status"], o["csent"], o["why"] = "cancelling", now, o["why"] or o["want_cancel"] or why
            ids.append(o["oid"])
        if not ids:
            return
        self.lstat["cancels"] += len(ids)
        fn = self.gw.cancel_batch if self.gw is not None else self._dry_cancel
        fut = self._live_call(fn, ids)
        fut.add_done_callback(lambda f, ids=ids: self._live_cancelled(f, ids))

    def _live_cancelled(self, f, ids):
        now = self.clock() * 1000.0
        try:
            r = f.result()
        except Exception as e:                         # still resting: the next decision cancels them again
            self.lstat["cancel_errors"] += 1
            self._lcretry = now + 200
            self._err(f"live cancel: {type(e).__name__}: {e}"[:200])
            for oid in ids:
                o = self.loid.get(oid)
                if o is not None and o["status"] == "cancelling":
                    o["status"], o["csent"] = "live", None
            return
        if r.get("ms") is not None:
            self.lcancel_ms.append(r["ms"])
        for oid in list(r["canceled"]) + list(r["not_canceled"]):
            o = self.loid.get(oid)
            if o is None:
                continue
            if o["status"] == "cancelling":
                o["status"] = "done"
            o["cack"] = o["cack"] or now
            if oid in r["not_canceled"]:
                o["code"] = o["code"] or "not_canceled"
                o["err"] = str(r["not_canceled"][oid])[:120]

    def _dry_post(self, orders, _origin):
        return {"orders": [{"ok": True, "order_id": f"dry-{time.time_ns()}-{i}", "error": None, "code": None,
                            "status": "live"} for i in range(len(orders))], "post_ms": 30.0}

    @staticmethod
    def _dry_cancel(ids):
        return {"canceled": list(ids), "not_canceled": {}, "ms": 30.0}

    # ------------------------------------------------------------------ live: fills, ledger
    def _lwin(self, o):
        w = self.live.get(o["slug"])
        if w is None:
            w = self.live[o["slug"]] = {"slug": o["slug"], "win": o["win"], "start": o["start"], "end": o["end"],
                                        "orders": 0, "fills": 0, "up_sh": 0.0, "up_cost": 0.0, "down_sh": 0.0,
                                        "down_cost": 0.0, "rebate": 0.0, "winner": None, "pnl": None, "merge": None}
        return w

    def _on_live_event(self, e):
        """User-channel event (on the loop): our maker fills, by order id."""
        now = self.clock() * 1000.0
        et = e.get("event_type")
        if et == "trade":
            status = str(e.get("status") or "").upper()
            for mo in e.get("maker_orders") or ():
                o = self.loid.get(str(mo.get("order_id")))
                if o is None:
                    continue
                key = (e.get("id"), o["oid"])
                try:
                    amt = float(mo.get("matched_amount") or 0)
                except (TypeError, ValueError):
                    continue
                if status == "FAILED":
                    if key in self._ltrades:          # the match did not settle: take it back
                        self._ltrades.discard(key)
                        o["tsum"] -= amt
                        self.lstat["fills_failed"] += 1
                        self._live_matched(o, o["tsum"], now, e.get("id"), force=True)
                    continue
                if key in self._ltrades:
                    continue
                self._ltrades.add(key)
                o["tsum"] += amt
                self._live_matched(o, max(o["matched"], o["tsum"]), now, e.get("id"))
        elif et == "order":
            o = self.loid.get(str(e.get("id")))
            if o is None:
                return
            try:
                sm = float(e.get("size_matched") or 0)
            except (TypeError, ValueError):
                sm = 0.0
            if sm > o["matched"] + 1e-9:
                self._live_matched(o, sm, now, None)
            if e.get("type") == "CANCELLATION" and o["status"] != "done":
                o["status"], o["cack"] = "done", o["cack"] or now

    def _live_matched(self, o, total, now, trade, force=False):
        d = total - o["matched"]
        if abs(d) < 1e-9 or (d < 0 and not force):
            return
        o["matched"] = total
        w = self._lwin(o)
        p, side = o["price"], o["side"]
        m = self.markets.get(o["slug"])
        rate = m.fee_rate if m is not None and m.fee_rate is not None else self.cfg.fee_rate
        w[f"{side}_sh"] += d
        w[f"{side}_cost"] += d * p
        w["rebate"] += d * rate * p * (1 - p) * self.cfg.rebate_share
        if d > 0:
            w["fills"] += 1
            self.lstat["fills"] += 1
            self.lstat["fill_shares"] += d
        if total >= o["size"] - 1e-6 and o["status"] in ("live", "cancelling"):
            o["status"] = "done"
        if m is not None:
            _ist(m)["lfills"].append((round(now, 1), o["oid"], side, p, round(d, 6), trade))
        self._live_log("fill", o, shares=d)
        if not str(o["oid"]).startswith("dry-"):
            db.x("UPDATE live_orders SET filled = ? WHERE order_id = ?", (o["matched"], o["oid"]))
        self._lrisk = self._live_risk()
        if m is not None:
            self._requote(m, now, force=True)          # inventory changed: the cap may bite

    def _live_risk(self):
        """(settled live trading PnL, worst case of the open windows = their PnL if the worse side wins)."""
        realized = worst = 0.0
        for w in self.live.values():
            if w["pnl"] is not None:
                realized += w["pnl"]
            else:
                worst += min(w["up_sh"], w["down_sh"]) - w["up_cost"] - w["down_cost"]
        return realized, worst

    def _live_log(self, kind, o, shares=None, info=None):
        self.live_log.append({"ts": time.time(), "kind": kind, "slug": o["slug"], "win": o["win"], "side": o["side"],
                              "price": o["price"], "size": o["size"], "shares": shares, "info": info,
                              "ms": o.get("post_ms")})

    def _live_sweep(self, m):
        def done(f):
            try:
                n = len(f.result()["canceled"])
                self.lstat["sweep_cancelled"] += n
                if n:
                    self._err(f"live sweep {m.slug}: cancelled {n} order(s) still open after quoting stopped")
            except Exception as e:
                self.lstat["sweep_errors"] += 1
                self._err(f"live sweep {m.slug}: {type(e).__name__}: {e}"[:200])
        self._loop.run_in_executor(self.gw.pool, self.gw.cancel_market, m.condition).add_done_callback(done)

    def _live_merge(self, m, w):
        w["merge"] = "sending"

        def done(f):
            try:
                w["merge"] = "merged " + str(f.result())[:60]
                self.lstat["merges"] += 1
            except Exception as e:
                w["merge"] = f"failed: {type(e).__name__}: {e}"[:120]
                self.lstat["merge_errors"] += 1
                self._err(f"live merge {m.slug}: {type(e).__name__}: {e}"[:200])
        self._loop.run_in_executor(self.gw.bg, self.gw.merge_max, m.condition).add_done_callback(done)

    def _load_live_maker(self):
        """Rebuild the live ledger of the current account from this bot's filled orders."""
        rows = db.q("SELECT slug, outcome, price, filled FROM live_orders WHERE origin = ? AND order_type = 'limit' "
                    "AND filled > 0 AND ts >= ?", (self.bot_id, self.account_since or 0.0))
        for r in rows:
            try:
                win, start = int(r["slug"].split("-")[-2].rstrip("m")), int(r["slug"].rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                continue
            o = {"slug": r["slug"], "win": win, "start": start, "end": start + win * 60}
            w = self._lwin(o)
            p, sh = float(r["price"]), float(r["filled"])
            w[f"{r['outcome']}_sh"] += sh
            w[f"{r['outcome']}_cost"] += sh * p
            w["rebate"] += sh * self.cfg.fee_rate * p * (1 - p) * self.cfg.rebate_share
            w["fills"] += 1
            w["merge"] = "restored"
        self._lrisk = self._live_risk()

    # ------------------------------------------------------------------ live: process
    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        src = set(self.cfg.sources)
        feeds = [self._coinbase(), self._chainlink()]
        if "binance" in src or self.cfg.spot_source != "coinbase":
            feeds.append(self._binance())
        if "kraken" in src:
            feeds.append(self._kraken())
        if "binance_fut" in src:
            feeds.append(self._binance_fut())
        if self.mode == "live":
            from ...trading.gateway import Gateway
            self.gw = Gateway()
            self.gw.armed = True                                # mode 'live' is the arming
            self.gw.light = True
            self.gw.on_event = self._on_live_event
            self.gw.start(self._loop)
            feeds += [self._live_resolver(), self._live_boot()]
        elif self.dry:
            self._lready = True
        tasks = [asyncio.create_task(c) for c in (*feeds, self._scheduler(), self._ticker(), self._clock(),
                                                  self._resolver(), self._official_prices())]
        try:
            await self._stop.wait()
        finally:
            if self.mode == "live":
                await self._live_shutdown()
            for t in tasks + list(self._ws_tasks.values()):
                t.cancel()
            self._ws_tasks.clear()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _live_boot(self):
        """Wait for the trading client, then cancel any order an earlier run of this bot left resting."""
        for _ in range(600):
            if self.gw.enabled:
                break
            await asyncio.sleep(0.1)
        if not self.gw.enabled:
            self._err(f"live: trading client not ready: {self.gw.reason}")
            return
        rows = db.q("SELECT order_id FROM live_orders WHERE origin = ? AND order_type = 'limit' AND ts > ? AND "
                    "(status IS NULL OR status NOT IN ('CANCELLATION', 'rejected'))", (self.bot_id, time.time() - 6 * 3600))
        ids = [r["order_id"] for r in rows if not str(r["order_id"]).startswith("rejected")]
        for i in range(0, len(ids), 500):
            try:
                r = await self._loop.run_in_executor(self.gw.pool, self.gw.cancel_batch, ids[i:i + 500])
                self.lstat["boot_cancelled"] += len(r["canceled"])
            except Exception as e:
                self._err(f"live: cancelling leftovers: {type(e).__name__}: {e}"[:200])
        self._lready = True

    async def _live_shutdown(self):
        """Cancel every live order before the process ends (UI stop, SIGTERM)."""
        self._lready = False
        ids = [o["oid"] for o in self.lorders.values() if o["oid"] and o["status"] in ("live", "cancelling")]
        if not ids or self.gw is None or not self.gw.enabled:
            return
        try:
            r = await asyncio.wait_for(self._loop.run_in_executor(self.gw.pool, self.gw.cancel_batch, ids), 3.0)
            self.lstat["stop_cancelled"] += len(r["canceled"])
        except Exception as e:
            self._err(f"live: cancel on stop: {type(e).__name__}: {e}"[:200])

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
        lv = m.__dict__.get("lv")
        if self.live_on and lv:
            rows = []
            for lid in lv["lids"]:
                o = self.lorders.get(lid)
                if o is not None:
                    rows.append(tuple(o.get(k) for k in ("lid", "oid", "side", "price", "size", "sent", "ack",
                                                          "post_ms", "status", "csent", "cack", "matched", "why",
                                                          "code", "err")))
            rec["live"] = {"mode": "dry" if self.dry else "live", "ledger": self.live.get(m.slug),
                           "orders": {"cols": LIVE_ORDER_COLS, "rows": rows},
                           "fills": {"cols": LIVE_FILL_COLS, "rows": st["lfills"]}}
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
                tmp.write_bytes(gzip.compress(orjson.dumps(rec, default=str), compresslevel=6))
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
        s["mode"] = self.mode
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
        if self.live_on:
            s["live"] = self._live_snapshot(now)

    def _live_snapshot(self, now):
        cfg, gw = self.cfg, self.gw
        realized, worst = self._lrisk
        done = sorted((w for w in self.live.values() if w["pnl"] is not None), key=lambda w: w["end"])
        series, cum = [[self.account_since or (done[0]["start"] if done else now), 0.0]], 0.0
        for w in done:
            cum += w["pnl"]
            series.append([w["end"], round(cum, 4)])
        resting = [o for o in self.lorders.values() if o["status"] in ACTIVE]
        block = self._live_block(None)
        rej = {k[4:]: v for k, v in self.lstat.items() if k.startswith("rej_")}
        return {"kind": "maker", "dry": self.dry, "enabled": True if self.dry else bool(gw and gw.enabled),
                "reason": "" if self.dry else (gw.reason if gw else "starting"), "paused": cfg.live_paused,
                "state": block, "usdc": gw.balance if gw else None, "realized": realized,
                "rebate": sum(w["rebate"] for w in self.live.values()), "worst_open": worst,
                "at_risk": max(0.0, -worst), "open_cost": sum(w["up_cost"] + w["down_cost"] for w in self.live.values()
                                                             if w["pnl"] is None),
                "loss_limit": cfg.live_loss_limit_usd, "room": cfg.live_loss_limit_usd + realized + worst,
                "halted": block == "loss_limit", "spend_cap": cfg.spend_cap_usd, "merge": cfg.live_merge,
                "windows_done": len(done), "windows_won": sum(1 for w in done if w["pnl"] > 0),
                "spent": sum(w["up_cost"] + w["down_cost"] for w in self.live.values()),
                "resting": len(resting), "resting_usd": sum((o["size"] - o["matched"]) * o["price"] for o in resting),
                "sent": self.lstat["sent"], "accepted": self.lstat["live"], "rejected": sum(rej.values()), "rej": rej,
                "cancels": self.lstat["cancels"], "fills": self.lstat["fills"],
                "fill_shares": self.lstat["fill_shares"], "counts": dict(self.lstat),
                "post_ms": _pct(list(self.lpost_ms), 0.5), "post_ms_p90": _pct(list(self.lpost_ms), 0.9),
                "cancel_ms": _pct(list(self.lcancel_ms), 0.5), "pnl_series": series,
                "windows": sorted(self.live.values(), key=lambda w: -w["start"])[:60],
                "orders": [{"slug": o["slug"], "win": o["win"], "side": o["side"], "price": o["price"], "size": o["size"],
                            "matched": o["matched"], "status": o["status"], "age": now - o["sent"] / 1000.0}
                           for o in sorted(resting, key=lambda o: (o["slug"], o["side"], -o["price"]))][:60],
                "log": list(self.live_log)[-100:],
                "gw_errors": list(gw.errors)[-5:] if gw else [], "gw_reconnects": gw.reconnects if gw else 0}

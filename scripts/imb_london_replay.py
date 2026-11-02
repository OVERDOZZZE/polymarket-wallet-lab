"""Phase 3 of docs/bots/imb_maker/LONDON_PLAN.md: replay imb_maker configs with London latencies and pessimistic fills.

    .venv\\Scripts\\python -m scripts.imb_london_replay --from "2026-10-25 00:00" --to "2026-10-25 12:00"
        [--dir data/recordings_server] [--workers 11] [--only name,name]

Engines (subclasses of ImbMakerBot, nothing changed in the strategy):
  fill  queue  the paper model (queue ahead at placement, moved up by prints and pro-rata cancels)
        pq     pessimistic queue: shares joining our level within JOIN_MS of our decision go ahead of us
               (the post-mortem: twin fills sat at levels that grew 4x in the next 60 ms)
        tt     trade-through: a bid fills only on a print strictly better for the taker than our price
  timing  "london": recordings made in Dublin re-timed to London's feeds (Polymarket book 8 ms earlier, Binance
          futures 21 ms later: latency_profile sites london vs dublin, feed p50); "dublin": as recorded
  latency_ms / cancel_latency_ms = decision -> order on the book / cancel effective: London calm 30 / 23
          (order_speed_probe: on the book p50 27, off the book p50 20, + ~3 ms decide/sign), London stressed 30 / 60
          (cancels slowing ~3x during moves, as Dublin's did), Dublin reference 60 / 90 (post-mortem).
Per variant: windows, fills, shares, net PnL (official winners + rebate), c/share, share-weighted markout at 1 s and
10 s (token mid - fill price, cents). Results -> data/replays/imb_london-<from>.json. Places no orders.
"""
import argparse
import collections
import heapq
import json
import math
import re
import sys
import time
from statistics import NormalDist
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.bots.strategies.imb_maker import BinanceGuard, ImbMakerBot  # noqa: E402
from backend.config import DATA_DIR  # noqa: E402

JOIN_MS = 60.0
SLOW = {}                                      # variant name -> cancel ms while a guard spike is active (per process)
FAIR = {}                                      # variant name -> fair-value guard threshold (probability units)
SWEEP = {}                                     # variant name -> P(sweep) threshold (scripts/sweep_predict model)
_SW = {"model": None, "cache": {}}
GATE = {}                                      # variant name -> activity gate: book messages / 500 ms that pull all
EMP_FILE = "data/probe/stress-20261101-095208.json"   # cancel_stress_probe run: off-the-book ms, calm vs spike
_EMP = {}
ACT_BUCKETS = ((0, 30), (30, 100), (100, 10 ** 9))   # book messages / 500 ms (stress probe: n 209 / 50 / 52)


def emp_lat():
    """{"calm": [...], "spike": [...]}: measured cancel-sent -> off-the-book ms (+3 ms decide / sign)."""
    if not _EMP:
        from scripts.cancel_stress_probe import off_book
        d = json.loads(Path(EMP_FILE).read_text())
        offs = off_book(d["rows"], d["ev"])
        for k in ("calm", "spike"):
            _EMP[k] = [offs[r["oid"]] + 3.0 for r in d["rows"] if r["kind"] == k and r["oid"] in offs]
        for b, (lo, hi) in enumerate(ACT_BUCKETS):     # by book messages of that market around the cancel
            _EMP[f"act{b}"] = [offs[r["oid"]] + 3.0 for r in d["rows"] if r["oid"] in offs
                               and lo <= (r.get("book_msgs_500ms") or 0) < hi]
    return _EMP


class _Lat(float):
    """A cancel latency that tells Order.live to use the order's own drawn latency (emp variants)."""
    bot = None


def _live(self, now, latency):
    if self.cancel_ms is None:
        return now >= self.active_ms
    if isinstance(latency, _Lat):
        latency = latency.bot.draw(self)
    return now >= self.active_ms and (self.cancel_ms is None or now < self.cancel_ms + latency)


replay.registry  # noqa: B018  (engine module loaded below)
from backend.bots.strategies import market_maker as _mm  # noqa: E402
_mm.Order.live = _live


class FairGuard(BinanceGuard):
    """Delta-aware guard: fires when the 1 s futures move x the active window's option sensitivity moves the Up fair
    value by >= thr (sensitivity = pdf(inv_cdf(Up mid)) / (realized vol x sqrt(time left)); no strike needed)."""

    def __init__(self, bps, win_ms, cool_ms, thr, scale):
        super().__init__(bps, win_ms, cool_ms)
        self.thr, self.scale = thr, scale
        self.sec, self.rets = {}, collections.deque()    # last price per second; (second, 1 s log return bps)

    def vol(self, t):
        xs = [r for s_, r in self.rets]
        return max(0.3, (sum(x * x for x in xs) / len(xs)) ** 0.5) if len(xs) > 30 else 0.6

    def on_tick(self, t, price):
        s_ = int(t // 1000)
        if s_ not in self.sec:
            if s_ - 1 in self.sec:
                self.rets.append((s_, math.log(price / self.sec[s_ - 1]) * 1e4))
            self.sec = {k: v for k, v in self.sec.items() if k >= s_ - 2}
            while self.rets and self.rets[0][0] < s_ - 300:
                self.rets.popleft()
        self.sec[s_] = price
        self.ticks.append((t, price))
        while len(self.ticks) >= 2 and self.ticks[1][0] <= t - self.win_ms:
            self.ticks.popleft()
        t0, p0 = self.ticks[0]
        if t0 > t - self.win_ms:
            return 0
        k = self.scale(t)
        r = (price / p0 - 1) * 1e4
        if k is None or abs(r * k / self.vol(t)) < self.thr:
            return 0
        side = 1 if r < 0 else -1
        self.until[side] = max(self.until[side], t + self.cool_ms)
        return side
SHIFT_MS = {"bf": 21.0, "bk": -8.0}           # London - Dublin feed p50 (Binance futures, Polymarket book)
_ORIG_ITER = replay.iter_recording
_N = NormalDist()

IMB10 = {"bankroll": 10.0, "windows": [5], "B": 5.0, "n_levels": 1, "inv_cap": 6.0, "min_price": 0.3,
         "max_price": 0.7, "spend_cap_usd": 6.0, "spend_cap_hard": True, "exposure_cap": True, "imb_join": 0.4,
         "back_ticks": 2, "spike_bps": 0.5, "lookback_ms": 1000.0, "pull_ms": 1000.0, "feed_stale_ms": 10000.0,
         "start_delay_s": 5.0, "stop_before_end_s": 25.0, "rebate_share": 0.2, "fee_rate": 0.07}
IMB300 = {**IMB10, "bankroll": 300.0, "n_levels": 13, "inv_cap": 25.0, "min_price": 0.1, "max_price": 0.99,
          "spend_cap_usd": 0.0, "spend_cap_hard": False}
LAT = {"calm": {"latency_ms": 30.0, "cancel_latency_ms": 23.0},
       "stress": {"latency_ms": 30.0, "cancel_latency_ms": 60.0},
       "dub": {"latency_ms": 60.0, "cancel_latency_ms": 90.0}}


class _Imb(ImbMakerBot):
    FILL = "queue"

    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self._mk1 = collections.deque()
        import random
        self._rng = random.Random(bot_id)
        self._emp_c, self._spk = {}, collections.deque()
        self._spk_g = BinanceGuard(0.5, 1000.0, 1000.0)
        self._msgs, self._gate_until = {}, -1e18
        self._sw_hist = collections.defaultdict(collections.deque)    # (slug, side) -> (ms, best bid, size at it)
        self._sw_since = {}                                           # (slug, side) -> (best bid, since ms)
        self._sw_tr = collections.defaultdict(collections.deque)      # (slug, side) -> (ms, print size)
        self._bfq = collections.deque()                               # Binance futures (recv ms, price), 3 s

    def _fill(self, m, o, shares, price, now, maker):
        super()._fill(m, o, shares, price, now, maker)
        self._mk1.append((now + 1000.0, m, o.side, price, self.fills[-1]))

    def _tick(self, now):
        if "-emp" in self.bot_id:                      # each cancelled order leaves at its own drawn latency
            for m in self.markets.values():
                if m.orders:
                    m.orders = [o for o in m.orders if o.taker or o.cancel_ms is None or now < o.cancel_ms + self.draw(o)]
        super()._tick(now)
        while self._mk1 and self._mk1[0][0] <= now:
            _, m, side, price, rec = self._mk1.popleft()
            mid = m.mid(side)
            if mid is None and m.state == "settled" and m.winner:
                mid = 1.0 if m.winner == side else 0.0
            if mid is not None:
                rec["mk1"] = round((mid - price) * 100, 3)

    def _scale(self, t):
        """pdf(inv_cdf(Up mid)) / sqrt(seconds left) of the active 5m window (FairGuard divides by the vol)."""
        for m in self.markets.values():
            if m.start <= t / 1000.0 < m.end:
                mid = m.mid("up")
                tau = m.end - t / 1000.0
                if mid is None or tau < 5 or not 0.03 < mid < 0.97:
                    return None
                return _N.pdf(_N.inv_cdf(mid)) / math.sqrt(tau)
        return None

    def _on_ex(self, src, price, recv):
        if src == "binance_fut" and (SWEEP.get(self.bot_id)):
            self._bfq.append((recv, price))
            while self._bfq and self._bfq[0][0] < recv - 3000.0:
                self._bfq.popleft()
        if src == "binance_fut" and "-emp" in self.bot_id:
            if self._spk_g.on_tick(recv, price):
                if self._spk and recv <= self._spk[-1][1]:
                    self._spk[-1] = (self._spk[-1][0], recv + 1000.0)
                else:
                    self._spk.append((recv, recv + 1000.0))
                    while len(self._spk) > 50:
                        self._spk.popleft()
        thr = FAIR.get(self.bot_id)
        if thr and src in self.cfg.sources and src not in self.guards:
            self.guards[src] = FairGuard(self.cfg.spike_bps, self.cfg.lookback_ms, self.cfg.pull_ms, thr, self._scale)
        super()._on_ex(src, price, recv)

    def draw(self, o):
        """This order's cancel latency: drawn once per cancel from the measured calm / spike distribution; spike =
        a 0.5 bps / 1 s Binance futures move (the stress probe's own trigger) within the last second at cancel time."""
        c = self._emp_c.get(id(o))
        if c is None or c[0] != o.cancel_ms:
            t = o.cancel_ms
            if "-empa" in self.bot_id:                 # activity of the order's market in the 500 ms before
                n = self._act(self._mkt_of(o), t)
                key = f"act{next(b for b, (lo, hi) in enumerate(ACT_BUCKETS) if lo <= n < hi)}"
            else:
                key = "spike" if any(a <= t <= b for a, b in self._spk) else "calm"
            off = 8.0 if ("-empd" in self.bot_id or "-dub" in self.bot_id) else 0.0   # Dublin: ~8 ms farther
            mult = float(self.bot_id.rsplit("-x", 1)[1]) if "-x" in self.bot_id else 1.0   # bot pipeline stand-in
            c = self._emp_c[id(o)] = (t, (self._rng.choice(emp_lat()[key]) + off) * mult)
        return c[1]

    def _mkt_of(self, o):
        for m in self.markets.values():
            if o in m.orders:
                return m.slug
        return None

    def _act(self, slug, t):
        q = self._msgs.get(slug)
        return sum(1 for x in q if t - 500.0 <= x <= t) if q else 0

    def _on_book_msg(self, m, d):
        t = self._t * 1000.0
        q = self._msgs.setdefault(m.slug, collections.deque())
        q.append(t)
        while q and q[0] < t - 1500.0:
            q.popleft()
        n = GATE.get(self.bot_id)
        if n and self._act(m.slug, t) >= n:            # the book is getting busy: out before the queue builds
            self._gate_until = t + 1000.0
            if any(o.cancel_ms is None and not o.taker for o in m.orders):
                self.counts["gate_pulls"] += 1
                self._pull_all(m, t, "gate")
        super()._on_book_msg(m, d)
        if SWEEP.get(self.bot_id):
            for side in ("up", "down"):
                b = m.best_bid(side)
                q1 = m.books[side]["bids"].get(b, 0.0) if b is not None else 0.0
                k = (m.slug, side)
                if self._sw_since.get(k, (None,))[0] != b:
                    self._sw_since[k] = (b, t)
                h = self._sw_hist[k]
                h.append((t, b, q1))
                while h and h[0][0] < t - 2500.0:
                    h.popleft()

    def _sweep_p(self, m, side, now):
        """P(the best bid level of `side` is gone within 50..1000 ms), scripts/sweep_predict features; cached per
        market, side and 100 ms (market data only, so shared by all variants of the process)."""
        key = (m.slug, side, int(now // 100))
        if key in _SW["cache"]:
            return _SW["cache"][key]
        bids, asks = m.books[side]["bids"], m.books[side]["asks"]
        b, k = m.best_bid(side), m.best_ask(side)
        t_in = now / 1000.0 - m.start
        if b is None or k is None or not 0.05 <= b <= 0.95 or not 5 <= t_in <= 275:
            return None, None
        q1, a1 = bids.get(b, 0.0), asks.get(k, 0.0)
        q2 = bids.get(round(b - 0.01, 2), 0.0)
        q3 = sum(bids.get(round(b - 0.01 * j, 2), 0.0) for j in (1, 2, 3))
        h = self._sw_hist[(m.slug, side)]

        def ago(dt):
            for hm, hb, hq in reversed(h):
                if hm <= now - dt:
                    return hq if hb == b else 0.0
            return q1
        tr = self._sw_tr[(m.slug, side)]
        while tr and tr[0][0] < now - 2000.0:
            tr.popleft()

        def bf(t):
            v = None
            for r, p in self._bfq:
                if r <= t:
                    v = p
                else:
                    break
            return v
        f0, f5, f2 = bf(now), bf(now - 500.0), bf(now - 2000.0)
        sg = 1 if side == "up" else -1
        x = [q1, q2, q3, a1, round((k - b) * 100), q1 / (q1 + a1) if q1 + a1 > 0 else 0.5,
             min(now - self._sw_since.get((m.slug, side), (b, now))[1], 60000.0), q1 - ago(500.0), q1 - ago(2000.0),
             sum(z for tm, z in tr if tm >= now - 500.0), sum(z for _, z in tr), self._act(m.slug, now),
             sg * 1e4 * (f0 / f5 - 1) if f0 and f5 else 0.0, sg * 1e4 * (f0 / f2 - 1) if f0 and f2 else 0.0, b, t_in]
        if _SW["model"] is None:
            import pickle
            with open(DATA_DIR / "forensics" / "sweep_model.pkl", "rb") as fh:
                obj = pickle.load(fh)
            _SW["model"], _SW["reg"] = obj["model"], obj.get("reg")
        if len(_SW["cache"]) > 20000:
            _SW["cache"].clear()
        p = float(_SW["model"].predict_proba([x])[0, 1])
        v = float(_SW["reg"].predict([x])[0]) if _SW.get("reg") is not None else None
        _SW["cache"][key] = (p, v)
        return p, v

    def _guard_allow(self, now):
        up, dn, stale = super()._guard_allow(now)
        if now < self._gate_until:
            return False, False, stale
        thr = SWEEP.get(self.bot_id)
        if thr:
            for m in self.markets.values():
                if m.start <= now / 1000.0 < m.end and m.minutes == 5:
                    (pu, vu), (pd, vd) = self._sweep_p(m, "up", now), self._sweep_p(m, "down", now)
                    if up and pu is not None and pu >= thr:
                        up = False
                        self.counts["sweep_block_up"] += 1
                    if dn and pd is not None and pd >= thr:
                        dn = False
                        self.counts["sweep_block_down"] += 1
                    break
        return up, dn, stale

    def _cancel_lat(self):
        if "-emp" in self.bot_id:
            x = _Lat(3000.0)        # the engine prunes cancelled orders after this; _tick prunes each at its own draw
            x.bot = self
            return x
        return self._cancel_lat_slow()

    def _cancel_lat_slow(self):
        """SLOW[bot id] ms while any guard is in its post-spike cooldown (the market is moving), else the config's."""
        slow = SLOW.get(self.bot_id)
        if slow and any(self._t * 1000.0 < max(g.until.values()) for g in self.guards.values()):
            return slow
        return super()._cancel_lat()

    def _settle_levels(self, m, now):
        """pqb: shrinks that no print explains (cancels) never move us up the queue (assumed behind us)."""
        if self.FILL != "pqb":
            return super()._settle_levels(m, now)
        m.shrinks = [sh for sh in m.shrinks if now - sh[0] < _mm.LEVEL_MATCH_MS]
        for k in [k for k, v in m.trade_credit.items() if now - v[0] > _mm.LEVEL_MATCH_MS]:
            m.trade_credit.pop(k)

    def _level_change(self, m, side, px, old, new, now):
        if self.FILL in ("pq", "pqb") and new > old + 1e-9:
            for o in self._at(m, side, px):
                if now - o.placed_ms <= JOIN_MS:
                    o.queue += new - old
        super()._level_change(m, side, px, old, new, now)

    def _on_print(self, m, side, price, size, taker_side, now):
        if SWEEP.get(self.bot_id):
            self._sw_tr[(m.slug, side)].append((now, size))
        if self.FILL != "tt":
            return super()._on_print(m, side, price, size, taker_side, now)
        lat = self._cancel_lat()
        if taker_side == "SELL":
            self._match_print(m, (side, round(price, 4)), size, now)
        elif taker_side == "BUY":
            self._match_print(m, ("down" if side == "up" else "up", round(1.0 - price, 4)), size, now)
        cands = []
        for o in m.orders:
            if o.taker or o.remaining <= 1e-9 or not o.live(now, lat):
                continue
            if o.side == side and taker_side == "SELL":
                eq = price
            elif o.side != side and taker_side == "BUY":
                eq = round(1.0 - price, 6)
            else:
                continue
            if o.price > eq + 1e-9:                  # strictly through our price: no fills from our own level
                cands.append(o)
        left = size
        for o in sorted(cands, key=lambda o: (-o.price, o.placed_ms)):
            if left <= 1e-9:
                break
            take = min(o.remaining, left)
            left -= take
            self._fill(m, o, take, o.price, now, maker=True)
        m.orders = [o for o in m.orders if o.remaining > 1e-9]


class ImbQueue(_Imb):
    FILL = "queue"


class ImbPQ(_Imb):
    FILL = "pq"


class ImbTT(_Imb):
    FILL = "tt"


class ImbPQB(_Imb):
    FILL = "pqb"


ENGINES = {"queue": ImbQueue, "pq": ImbPQ, "tt": ImbTT, "pqb": ImbPQB}


def retimed(start_ts, end_ts, rec_dir=None):
    """iter_recording with London's feed delays: shifted receive times, re-sorted (shifts are < 30 ms)."""
    h, seq = [], 0
    for ms, src, raw in _ORIG_ITER(start_ts, end_ts, rec_dir):
        t = ms + SHIFT_MS.get("bk" if src.startswith("bk:") else src, 0.0)
        heapq.heappush(h, (t, seq, src, raw))
        seq += 1
        while h and h[0][0] < ms - 10.0:
            t, _, s, r = heapq.heappop(h)
            yield t, s, r
    while h:
        t, _, s, r = heapq.heappop(h)
        yield t, s, r


def variants():
    """(name, timing, base config, overrides, fill). Kept small: each costs ~1 min of CPU per recorded hour.
    Names ending in -spike<ms>: cancels take <ms> while a guard spike is active (calm latency otherwise)."""
    v = [(f"10-{fill}-{lat}", "london", IMB10, LAT[lat], fill)
         for fill, lat in (("queue", "calm"), ("pq", "calm"), ("tt", "calm"), ("pq", "stress"), ("tt", "stress"))]
    v.append(("10-tt-dub-ref", "dublin", IMB10, LAT["dub"], "tt"))
    for cname, ov in (("back3", {"back_ticks": 3}), ("join06", {"imb_join": 0.6}),
                      ("guard03", {"spike_bps": 0.3, "pull_ms": 2000.0}), ("band46", {"min_price": 0.4, "max_price": 0.6})):
        v.append((f"10-{cname}-pq-calm", "london", IMB10, {**LAT["calm"], **ov}, "pq"))
    g03 = {"spike_bps": 0.3, "pull_ms": 2000.0}
    v.append(("10-guard03-pq-stress", "london", IMB10, {**LAT["stress"], **g03}, "pq"))
    for ms in (100, 1000):
        v += [(f"10-pq-spike{ms}", "london", IMB10, LAT["calm"], "pq"),
              (f"10-guard03-pq-spike{ms}", "london", IMB10, {**LAT["calm"], **g03}, "pq")]
    v += [("10-pq-emp", "london", IMB10, LAT["calm"], "pq"), ("10-tt-emp", "london", IMB10, LAT["calm"], "tt"),
          ("10-guard03-pq-emp", "london", IMB10, {**LAT["calm"], **g03}, "pq"),
          ("10-fair1-pq-emp", "london", IMB10, {**LAT["calm"], "pull_ms": 2000.0}, "pq"),
          ("10-fair05-pq-emp", "london", IMB10, {**LAT["calm"], "pull_ms": 2000.0}, "pq"),
          ("10-queue-emp", "london", IMB10, LAT["calm"], "queue"),
          ("10-pqb-emp", "london", IMB10, LAT["calm"], "pqb"),
          ("10-fair1-pqb-emp", "london", IMB10, {**LAT["calm"], "pull_ms": 2000.0}, "pqb"),
          ("10-guard03-pqb-emp", "london", IMB10, {**LAT["calm"], **g03}, "pqb")]
    v += [("10-pqb-empa", "london", IMB10, LAT["calm"], "pqb"), ("10-tt-empa", "london", IMB10, LAT["calm"], "tt"),
          ("10-gate30-pqb-empa", "london", IMB10, LAT["calm"], "pqb"),
          ("10-gate60-pqb-empa", "london", IMB10, LAT["calm"], "pqb"),
          ("10-gate30-tt-empa", "london", IMB10, LAT["calm"], "tt"),
          ("10-pqb-empa-dub", "dublin", IMB10, {"latency_ms": 35.0, "cancel_latency_ms": 35.0}, "pqb"),
          ("10-tt-empa-dub", "dublin", IMB10, {"latency_ms": 35.0, "cancel_latency_ms": 35.0}, "tt")]
    for fill in ("queue", "pq", "tt"):                 # Dublin as recorded: validation against 10-27's live run
        v.append((f"10-{fill}-empd", "dublin", IMB10, {"latency_ms": 35.0, "cancel_latency_ms": 35.0}, fill))
    for fill in ("pq", "pqb"):                         # calibration: cancel latency x2 / x3 (the bot's own pipeline)
        for mult in ("1", "2", "3"):
            v.append((f"10-{fill}-empd-x{mult}", "dublin", IMB10, {"latency_ms": 35.0, "cancel_latency_ms": 35.0}, fill))
    for thr in ("05", "1"):                            # fair-value guard 0.5 c / 1 c, pull 2 s
        for lat in ("calm", "spike100"):
            v.append((f"10-fair{thr}-pq-{lat}", "london", IMB10, {**LAT["calm"], "pull_ms": 2000.0}, "pq"))
    return v


L13 = {**IMB10, "bankroll": 100.0, "n_levels": 13, "min_price": 0.1, "max_price": 0.9, "inv_cap": 25.0,
       "exposure_cap": False, "spend_cap_usd": 30.0, "spend_cap_hard": False}


def variants_ladder():
    """Queue-priority test (trader_forensics 2026-11-01: our 1-level fills are 75% level sweeps; the traders' resting
    ladders fill on ordinary flow). Full Crouch-shape ladders at the 5-share minimum size, $100 budget."""
    lat, g03, ng = LAT["calm"], {"spike_bps": 0.3, "pull_ms": 2000.0}, {"spike_bps": 1e6}
    v = [("10-pqb-empa", "london", IMB10, lat, "pqb"), ("10-tt-empa", "london", IMB10, lat, "tt")]
    for name, ov in (("L13", {}), ("L13j0", {"imb_join": 0.0}), ("L6", {"n_levels": 6}), ("L13B10", {"B": 10.0}),
                     ("L13g03", g03), ("L13ng", ng), ("L13c50", {"inv_cap": 50.0})):
        v.append((f"{name}-pqb-empa", "london", L13, {**lat, **ov}, "pqb"))
    v.append(("L13-tt-empa", "london", L13, lat, "tt"))
    big = {"inv_cap": 1e6, "spend_cap_usd": 0.0}                     # Crouch: no inventory / spend cap
    v += [("L13nc-pqb-empa", "london", L13, {**lat, **big}, "pqb"), ("L13nc-queue-empa", "london", L13, {**lat, **big}, "queue")]
    cr = {"bankroll": 5000.0, "B": 60.0, "n_levels": 13, "inv_cap": 300.0, "min_price": 0.1, "max_price": 0.99,
          "spend_cap_usd": 0.0, "spend_cap_hard": False, "exposure_cap": False}       # BOT_SPEC = Crouch's own size
    v += [(f"CR-{fl}-empa", "london", IMB10, {**lat, **cr}, fl) for fl in ("pqb", "pq", "queue")]
    v += [("CRB5-pqb-empa", "london", IMB10, {**lat, **cr, "B": 5.0, "inv_cap": 25.0}, "pqb")]   # same, our size
    v += [("10-pq-empa", "london", IMB10, lat, "pq"), ("L13-pq-empa", "london", L13, lat, "pq"),
          ("L13j0-pq-empa", "london", L13, {**lat, "imb_join": 0.0}, "pq"),
          ("L13-queue-empa", "london", L13, lat, "queue")]
    v.append(("L13ng-tt-empa", "london", L13, {**lat, **ng}, "tt"))
    return v


def variants_sweep():
    """Sweep guard (scripts/sweep_predict model, trained 10-24 / 10-26 / 10-27): a side is not quoted while
    P(its best bid level is swept within 1 s) >= thr. Test on days the model has not seen (10-25, 11-01)."""
    lat = LAT["calm"]
    v = [("10-pqb-empa", "london", IMB10, lat, "pqb"), ("10-tt-empa", "london", IMB10, lat, "tt")]
    for thr in ("30", "40", "50", "60"):
        v.append((f"10-sw{thr}-pqb-empa", "london", IMB10, lat, "pqb"))
    v += [("10-sw40-tt-empa", "london", IMB10, lat, "tt"),
          ("10j0-sw40-pqb-empa", "london", IMB10, {**lat, "imb_join": 0.0}, "pqb"),
          ("10j0-pqb-empa", "london", IMB10, {**lat, "imb_join": 0.0}, "pqb")]
    j0 = {**lat, "imb_join": 0.0}
    v += [("10j0-sw30-pqb-empa", "london", IMB10, j0, "pqb"), ("10j0-sw50-pqb-empa", "london", IMB10, j0, "pqb"),
          ("10j0-sw40-tt-empa", "london", IMB10, j0, "tt")]
    for cut, fl in (("0", "pqb"), ("5", "pqb"), ("10", "pqb"), ("5", "tt"), ("0", "tt"), ("0", "queue"), ("0", "pq")):
        v.append((f"10j0-v{cut}-{fl}-empa", "london", IMB10, {**j0, "value_guard": True, "value_cut": int(cut) / 10.0}, fl))
    v.append(("10j0-tt-empa", "london", IMB10, j0, "tt"))
    # sticky quotes (keep the queue place while within N ticks below the wanted price), with / without the guard
    for n in (1, 2):
        for fl in ("pqb", "tt"):
            v.append((f"10j0-st{n}-{fl}-empa", "london", IMB10, {**j0, "sticky_ticks": n}, fl))
            v.append((f"10j0-st{n}-td0-{fl}-empa", "london", IMB10,
                      {**j0, "sticky_ticks": n, "value_guard": True, "value_cut": 0.0,
                       "value_model": "bid_value_thru_d4.json"}, fl))
            v.append((f"10-st{n}-{fl}-empa", "london", IMB10, {**lat, "sticky_ticks": n}, fl))
    # value given a fill (LONDON_PLAN 11-02): hi = given prints into the level, th = given prints through the queue
    for tag, model in (("hi", "bid_value_hit.json"), ("th", "bid_value_thru.json"), ("td", "bid_value_thru_d4.json")):
        for cut, fl in (("m10", "pqb"), ("m5", "pqb"), ("0", "pqb"), ("m5", "tt"), ("0", "tt")):
            c = -int(cut[1:]) / 10.0 if cut.startswith("m") else int(cut) / 10.0
            v.append((f"10j0-{tag}{cut}-{fl}-empa", "london", IMB10,
                      {**j0, "value_guard": True, "value_cut": c, "value_model": model}, fl))
    return v


def _chunk(args):
    vs, timing, start_ts, end_ts, rec_dir = args
    SLOW.update({n: float(n.rsplit("-spike", 1)[1]) for n, *_ in vs if "-spike" in n})
    FAIR.update({n: {"05": 0.005, "1": 0.01}[n.split("-")[1][4:]] for n, *_ in vs if n.split("-")[1].startswith("fair")})
    GATE.update({n: int(re.search(r"-gate(\d+)", n).group(1)) for n, *_ in vs if "-gate" in n})
    SWEEP.update({n: int(re.search(r"-sw(\d+)", n).group(1)) / 100.0 for n, *_ in vs if "-sw" in n})
    replay.iter_recording = retimed if timing == "london" else _ORIG_ITER
    res = replay.run([(n, "imb_maker", ov, base, ENGINES[fill]) for n, _t, base, ov, fill in vs],
                     start_ts, end_ts, rec_dir=rec_dir)
    return start_ts, {k: stats(r) for k, r in res.items()}


ADD = ("windows", "quoted_windows", "fills", "shares", "cost", "net", "rebate", "win_windows", "lose_windows",
       "mk1_w", "mk1_sh", "mk10_w", "mk10_sh", "paired", "pair_cost_w", "pair_pnl", "unpaired_pnl")


def stats(r):
    w, f = r["windows"], r["fills"]
    out = {"windows": len(w), "quoted_windows": sum(1 for x in w if x.get("cost")), "fills": len(f),
           "shares": sum(x["shares"] for x in f), "cost": sum(x["cost"] for x in w),
           "net": sum(x["net_pnl"] for x in w), "rebate": sum(x["rebate_est"] for x in w),
           "win_windows": sum(1 for x in w if x["net_pnl"] > 0), "lose_windows": sum(1 for x in w if x["net_pnl"] < 0)}
    for x in w:                                     # pairs: min(Up, Down) shares at the average costs (trader_forensics)
        up, dn = x.get("up_sh") or 0, x.get("down_sh") or 0
        m = min(up, dn)
        pp = m * (1 - x["up_cost"] / up - x["down_cost"] / dn) if m > 0 else 0.0
        out["paired"] = out.get("paired", 0) + m
        out["pair_cost_w"] = out.get("pair_cost_w", 0) + (m * (x["up_cost"] / up + x["down_cost"] / dn) if m > 0 else 0)
        out["pair_pnl"] = out.get("pair_pnl", 0) + pp
        out["unpaired_pnl"] = out.get("unpaired_pnl", 0) + ((x.get("trading_pnl") or 0) - pp)
    for key, lab in (("mk1", "mk1"), ("markout", "mk10")):
        xs = [(x[key], x["shares"]) for x in f if x.get(key) is not None]
        out[lab + "_w"], out[lab + "_sh"] = sum(a * b for a, b in xs), sum(b for _, b in xs)
    return out


def merge(parts):
    """Sum slice results; derived: c/share and share-weighted markouts."""
    t = {k: sum(p.get(k, 0) for p in parts) for k in ADD}
    t["c_per_share"] = round(t["net"] / t["shares"] * 100, 2) if t["shares"] else None
    t["mk1"] = round(t["mk1_w"] / t["mk1_sh"], 3) if t["mk1_sh"] else None
    t["mk10"] = round(t["mk10_w"] / t["mk10_sh"], 3) if t["mk10_sh"] else None
    t["pair%"] = round(200 * t["paired"] / t["shares"], 1) if t["shares"] else None
    t["pair_cost"] = round(t["pair_cost_w"] / t["paired"], 4) if t["paired"] else None
    t["pair_pnl"], t["unpaired_pnl"] = round(t["pair_pnl"], 2), round(t["unpaired_pnl"], 2)
    for k in ("shares", "cost", "net", "rebate"):
        t[k] = round(t[k], 2)
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--dir", default="data/recordings_server")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--slice-h", type=float, default=2.0, help="hours per process (all variants of a timing)")
    ap.add_argument("--period-h", type=float, default=12.0, help="report totals per this many hours")
    ap.add_argument("--only", default="")
    ap.add_argument("--set", default="", help="ladder: queue-priority ladders; sweep: sweep-guard variants")
    ap.add_argument("--native", action="store_true", help="recordings made in London: no re-timing")
    a = ap.parse_args()
    s, e = replay._ts(a.start), replay._ts(a.end)
    pick = {"ladder": variants_ladder, "sweep": variants_sweep}.get(a.set, variants)
    vs = [x for x in pick() if not a.only or x[0] in a.only.split(",")]
    if a.native:
        vs = [(n, "dublin", b, ov, fl) for n, _t, b, ov, fl in vs]          # "dublin" = as recorded
    step, lead = a.slice_h * 3600, 120                # a slice starts 2 min early: its first window is still traded
    chunks = []
    for timing in ("london", "dublin"):
        g = [x for x in vs if x[1] == timing]
        if g:
            chunks += [(g, timing, t - lead, min(t + step, e), a.dir) for t in range(int(s), int(e), int(step))]
    print(f"{len(vs)} variants, {len(chunks)} processes ({a.slice_h:g} h slices), {a.start} -> {a.end}", flush=True)
    t0, parts = time.time(), collections.defaultdict(list)     # (variant, period) -> slice stats
    from concurrent.futures import ProcessPoolExecutor, as_completed
    with ProcessPoolExecutor(max_workers=min(a.workers, len(chunks))) as ex:
        futs = [ex.submit(_chunk, c) for c in chunks]
        for i, fu in enumerate(as_completed(futs), 1):
            st, res = fu.result()
            per = int((st + lead - s) // (a.period_h * 3600))
            for k, r in res.items():
                parts[(k, per)].append(r)
            print(f"  {i}/{len(chunks)} slices done ({time.time() - t0:.0f} s)", flush=True)
    periods = sorted({p for _, p in parts})
    out = {n: {"all": merge([r for p in periods for r in parts[(n, p)]]),
               **{f"p{p}": merge(parts[(n, p)]) for p in periods}} for n, *_ in vs}
    d = DATA_DIR / "replays"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"imb_london-{a.start.replace(' ', '_').replace(':', '')}-{a.end.replace(' ', '_').replace(':', '')}.json"
    f.write_text(json.dumps({"from": a.start, "to": a.end, "dir": a.dir, "period_h": a.period_h,
                             "variants": {n: {"timing": t, "fill": fl, "overrides": ov} for n, t, _b, ov, fl in vs},
                             "results": out}, indent=1))
    keys = ("quoted_windows", "fills", "shares", "net", "c_per_share", "mk1", "mk10", "rebate", "pair%", "pair_cost",
            "pair_pnl", "unpaired_pnl")
    for per in ["all"] + [f"p{p}" for p in periods]:
        print(f"\n[{per}] {'variant':22s} " + " ".join(f"{k:>8s}" for k in keys))
        for n, *_ in vs:
            r = out[n][per]
            print(f"     {n:22s} " + " ".join(f"{'-' if r.get(k) is None else r[k]:>8}" for k in keys))
    print("saved", f)


if __name__ == "__main__":
    main()

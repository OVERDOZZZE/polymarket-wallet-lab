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

v2, `gate = "lock"` (2026-10-27 study, research/own_bots_2026-10-27): the window settles on the mean of the Chainlink
ticks observed in [end - 62, end - 3] (markets.twap_secs), so in its last minute most of the final price is already
known. A side is bid only while that final TWAP is locked in its favour: the ticks seen so far plus the spot for the
rest, projected as a random walk (the exact variance of a sum of path points), is at least `lock_z` sd and
`lock_usd` dollars past the strike (the dollar floor covers missing ticks: one window was decided by $0.008), the
market roughly agrees (mid >= lock_min_mid) and Chainlink is not silent. The side keeps its bids until z falls below
`lock_cancel_z`. Replaces the book-mid / model / jump checks. Why: below the 0.99 best bid a resting order fills
mostly when a reversal sweeps the book; once the outcome is locked, the sellers are holders cashing out early.
On 88 h of recordings (two periods, pessimistic queue) 0.99 bids this way were positive in both with no losing window.

Fills use the engine's queue-aware maker simulation (market_maker.py): we join the back of each level and fill only
after the shares ahead of us traded or were cancelled. That queue is the unknown the wallet data cannot show.
With queue_cancel_credit off, level shrinks without a trade never move us up (cancels assumed behind us): the
engine's pro rata credit proved optimistic exactly when a level is about to be swept (imb_maker post-mortem).
"""
import asyncio
import dataclasses
import math
import time
from collections import Counter, deque

from ... import db
from ...polymarket.markets import twap_secs
from .market_maker import LEVEL_MATCH_MS, BotConfig, PaperBot

LIVE_ACTIVE = ("sending", "live", "cancelling")


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
    # v2
    gate: str = "mid"                 # mid: book mid + price model (v1) | lock: final TWAP locked (v2)
    lock_z: float = 5.0               # arm a side once its projected final TWAP is this many sd past the strike ...
    lock_usd: float = 3.0             # ... and this many $ past it
    lock_cancel_z: float = 3.0        # disarm (pull the bids) below this
    lock_min_mid: float = 0.90        # the market must roughly agree: the side's book mid (else best bid) at least this
    lock_feed_ms: float = 6000.0      # no bids while the newest Chainlink tick is older than this
    lock_vol: str = "chainlink"       # sd of the projection: chainlink (its own 1 s ticks: what the TWAP averages; about
                                      # 0.6x Coinbase's) | engine (the Coinbase vol with its floor)
    lock_vol_s: float = 300.0         # chainlink: over this many seconds of ticks
    queue_cancel_credit: bool = True  # False: shrinks without a trade never move us up the queue (pessimistic)
    # live (bots.mode = 'live', read at start): the paper decision mirrored as real post-only bids
    live_size: float = 25.0           # shares per window (the whole window's bid; less once part of it filled)
    live_budget_usd: float = 50.0     # max USDC committed: resting bids + cost of filled, unsettled windows
    live_loss_limit_usd: float = 20.0  # no new bids once settled live trading PnL <= -this (one lost window stops it)
    live_paused: bool = False         # kill switch: every live order cancelled at once, none placed
    live_dry_run: bool = False        # paper mode: run the live order flow with simulated acks, send nothing
    live_heartbeat_s: float = 5.0     # send Polymarket's order heartbeat this often (0 = off). The account had a heartbeat
                                      # registration from another client: every lapse (10 s) cancelled all its open
                                      # orders, so bids lived ~16 s (2026-10-27). Keeping it alive stops that, and if
                                      # this bot dies the exchange cancels its bids within ~10 s.


def _covsum(ds):
    """sum over i, k of min(d_i, d_k): Var of a sum of random-walk points at times ds (unit variance per s)."""
    tot = 0.0
    n = len(ds)
    for i, d in enumerate(sorted(ds)):
        tot += d * (2 * (n - i) - 1)
    return tot


class LateFavBot(PaperBot):
    CONFIG = LateFavConfig
    STRATEGY = "late_fav"

    def _side_quotes(self, m, side, fair, now_s, imb):
        cfg = self.cfg
        if m.end - now_s > cfg.window_s or m.total_cost() >= cfg.max_window_cost:
            return []
        if cfg.gate == "lock":
            if not self._locked(m, side, now_s):
                return []
        else:
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

    # ------------------------------------------------------------------ v2: final TWAP lock
    def _lock_state(self, m, now_s):
        """(z, $ distance) of the projected final TWAP against the strike, signed for Up; None without data."""
        cfg = self.cfg
        if not self.cl or now_s * 1000.0 - self.cl[-1][0] > cfg.lock_feed_ms:
            return None
        K, S = self._strike(m), self._spot()
        if K is None or S is None:
            return None
        sig = self._cl_sigma(now_s) if cfg.lock_vol == "chainlink" else None
        if sig is None:
            sig = self._vol() * S
        tot, unseen, fut = 0.0, 0, []
        secs = twap_secs(m.end)
        for s in secs:
            if s in self._cl_exact:
                tot += self.cl_1s[s]
            elif s <= now_s:                  # observed already, not here yet (Chainlink arrives ~1.3 s late)
                tot += S
                unseen += 1
            else:
                tot += S
                fut.append(s - now_s)
        n = len(secs)
        mean = tot / n
        sd = sig * math.sqrt(_covsum(fut) + unseen) / n
        if sd <= 0:
            return (math.inf if mean >= K else -math.inf), mean - K
        return (mean - K) / sd, mean - K

    def _cl_sigma(self, now_s):
        """$ per sqrt(s) of Chainlink's 1 s ticks over the last lock_vol_s seconds (recomputed once a second), or None
        with fewer than 60 steps."""
        sec = int(now_s)
        c = self.__dict__.get("_lk_sig")
        if c is not None and c[0] == sec:
            return c[1]
        lo = sec - int(self.cfg.lock_vol_s)
        pts = [(s, self.cl_1s[s]) for s in range(lo, sec + 1) if s in self._cl_exact]
        d = [(b[1] - a[1]) / math.sqrt(b[0] - a[0]) for a, b in zip(pts, pts[1:])]
        sig = None
        if len(d) >= 60:
            mu = sum(d) / len(d)
            sig = math.sqrt(sum((x - mu) ** 2 for x in d) / len(d))
        self._lk_sig = (sec, sig)
        return sig

    def _locked(self, m, side, now_s):
        cfg = self.cfg
        st = m.__dict__.setdefault("lock", {"armed": None, "z": None})
        r = self._lock_state(m, now_s)
        if r is None:
            st["armed"] = None
            return False
        z, dist = r
        st["z"] = z
        zs, ds = (z, dist) if side == "up" else (-z, -dist)
        if st["armed"] == side:
            if zs < cfg.lock_cancel_z:
                st["armed"] = None
        elif zs >= cfg.lock_z and ds >= cfg.lock_usd:
            mid = m.mid(side)
            if mid is None:                   # a locked favourite usually has no asks left: judge by its best bid
                mid = m.best_bid(side)
            if mid is not None and mid >= cfg.lock_min_mid:
                st["armed"] = side
                self.counts["lock_armed"] += 1
        return st["armed"] == side

    # ------------------------------------------------------------------ fill simulation
    def _settle_levels(self, m, now):
        if self.cfg.queue_cancel_credit:
            return super()._settle_levels(m, now)
        if m.shrinks:                         # unexplained shrinks: cancels behind us, no queue credit
            m.shrinks = [sh for sh in m.shrinks if now - sh[0] < LEVEL_MATCH_MS]
        if m.trade_credit:
            for k in [k for k, v in m.trade_credit.items() if now - v[0] > LEVEL_MATCH_MS]:
                m.trade_credit.pop(k)

    # ------------------------------------------------------------------ live (bots.mode = 'live')
    def __init__(self, bot_id, cfg=None):
        super().__init__(bot_id, cfg)
        self.mode = "paper"
        if not self.offline:
            self.mode = (db.one("SELECT mode FROM bots WHERE id = ?", (bot_id,)) or {}).get("mode") or "paper"
        self.dry = self.mode != "live" and bool(self.cfg.live_dry_run) and not self.offline
        self.live_on = self.mode == "live" or self.dry
        self.gw = None
        self.lorders, self.loid, self._lid = {}, {}, 0      # local id -> order, exchange id -> order
        self.lwin = {}                                      # slug -> live window ledger
        self._ltrades = set()                               # (trade id, order id) counted
        self._lready = self.dry                             # live: gateway up and leftovers cancelled
        self._lblock = {}                                   # (slug, side, price) -> no new post before (ms)
        self._lsettle_ms = 0.0
        self.lstat = Counter()
        self.llog = deque(maxlen=100)
        if self.mode == "live":
            self._load_live()

    def _requote(self, m, now, force=False):
        super()._requote(m, now, force)
        if getattr(self, "live_on", False):
            self._live_sync(m, now)

    def _live_block(self):
        """Why no live bid may be placed right now, or None."""
        if not self._lready:
            return "starting"
        if self.cfg.live_paused:
            return "paused"
        if self.gw is not None and not self.gw.enabled:
            return "no_gateway"
        if self._realized() <= -self.cfg.live_loss_limit_usd + 1e-9:
            return "loss_limit"
        return None

    def _realized(self):
        return sum(w["pnl"] for w in self.lwin.values() if w["pnl"] is not None)

    def _committed(self):
        """USDC tied up: resting live bids + the cost of filled windows not settled yet."""
        rest = sum((o["size"] - o["matched"]) * o["price"] for o in self.lorders.values() if o["status"] in LIVE_ACTIVE)
        return rest + sum(w["up_cost"] + w["down_cost"] for w in self.lwin.values() if w["pnl"] is None)

    def _live_sync(self, m, now):
        """Bring this window's real bids to the paper decision (m.quotes): one bid per wanted (side, price), sized so the
        window never takes more than live_size shares, within live_budget_usd."""
        cfg = self.cfg
        mine = [o for o in self.lorders.values() if o["slug"] == m.slug and o["status"] in LIVE_ACTIVE]
        want = set()
        if self._live_block() is None and now / 1000.0 < m.end and m.state == "quoting":
            for side in ("up", "down"):
                for px, _sz in m.quotes.get(side) or ():
                    want.add((side, round(px, 4)))
        for o in mine:
            if (o["side"], o["price"]) not in want:
                self._live_cancel(o, now)
        if not want:
            return
        w = self.lwin.get(m.slug)
        taken = (w["up_sh"] + w["down_sh"]) if w else 0.0
        resting = sum(o["size"] - o["matched"] for o in mine)
        for side, px in sorted(want):
            if any(o["side"] == side and o["price"] == px for o in mine):
                continue
            if now < self._lblock.get((m.slug, side, px), 0.0):
                continue
            size = math.floor(cfg.live_size - taken - resting + 1e-9)
            room = math.floor((cfg.live_budget_usd - self._committed()) / px + 1e-9)
            if room < size:
                self.lstat["budget_capped"] += 1
                size = room
            if size < m.min_size:
                continue
            self._live_place(m, side, px, float(size), now)
            resting += size

    def _live_place(self, m, side, px, size, now):
        self._lid += 1
        o = {"lid": self._lid, "slug": m.slug, "start": m.start, "end": m.end, "side": side, "price": px, "size": size,
             "matched": 0.0, "tsum": 0.0, "status": "sending", "oid": None, "sent": now, "ack": None,
             "want_cancel": False, "cretry": 0.0, "err": None}
        self.lorders[o["lid"]] = o
        self.lstat["sent"] += 1
        if self.gw is None:                                  # dry run: answered after 30 ms, nothing sent
            fut = self._loop.create_future()
            self._loop.call_later(0.03, lambda: fut.done() or fut.set_result(
                {"orders": [{"ok": True, "order_id": f"dry-{time.time_ns()}", "error": None, "code": None}]}))
        else:
            fut = self._loop.run_in_executor(self.gw.pool, self.gw.post_limit_batch,
                                             [(m.slug, side, m.tokens[side], px, size)], self.bot_id)
        fut.add_done_callback(lambda f, o=o: self._live_placed(f, o))
        self._live_log("post", o)

    def _live_placed(self, f, o):
        now = self.clock() * 1000.0
        try:
            r = f.result()["orders"][0]
        except Exception as e:
            r = {"ok": False, "order_id": None, "error": f"{type(e).__name__}: {e}"[:200], "code": "exception"}
        o["ack"] = now
        if r["ok"]:
            o["oid"], o["status"] = str(r["order_id"]), "live"
            self.loid[o["oid"]] = o
            self.lstat["live"] += 1
            if o["want_cancel"]:
                self._live_cancel(o, now)
            return
        o["status"], o["err"] = "rejected", str(r.get("error") or "")[:200]
        low = o["err"].lower()
        cross = "cross" in low or "post-only" in low or "post only" in low or "marketable" in low
        self.lstat["rej_cross" if cross else "rej_other"] += 1
        # a failed post may still have landed (connection dropped): wait for the end-of-window sweep, do not stack
        self._lblock[(o["slug"], o["side"], o["price"])] = now + (300 if cross else 5000)
        self._live_log("rejected", o, info=o["err"])
        if not cross:
            self._err(f"live order rejected: {o['err']}")

    def _live_cancel(self, o, now):
        if o["status"] == "sending":
            o["want_cancel"] = True
            return
        if o["status"] != "live" or now < o["cretry"]:
            return
        o["status"] = "cancelling"
        self.lstat["cancels"] += 1
        if self.gw is None:
            fut = self._loop.create_future()
            self._loop.call_later(0.03, lambda: fut.done() or fut.set_result({"canceled": [o["oid"]], "not_canceled": {}}))
        else:
            fut = self._loop.run_in_executor(self.gw.cpool, self.gw.cancel_batch, [o["oid"]])
        fut.add_done_callback(lambda f, o=o: self._live_cancelled(f, o))

    def _live_cancelled(self, f, o):
        try:
            f.result()
        except Exception as e:                               # still resting: the next sync cancels it again
            self.lstat["cancel_errors"] += 1
            if o["status"] == "cancelling":
                o["status"], o["cretry"] = "live", self.clock() * 1000.0 + 500
            self._err(f"live cancel: {type(e).__name__}: {e}"[:200])
            return
        if o["status"] in LIVE_ACTIVE:
            o["status"] = "done"

    def _on_live_event(self, e):
        """User-channel event (on the loop): our fills, by order id (other bots on the wallet never mix in)."""
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
                    if key in self._ltrades:
                        self._ltrades.discard(key)
                        o["tsum"] -= amt
                        self._live_matched(o, o["tsum"], force=True)
                    continue
                if key in self._ltrades:
                    continue
                self._ltrades.add(key)
                o["tsum"] += amt
                self._live_matched(o, max(o["matched"], o["tsum"]))
        elif et == "order":
            o = self.loid.get(str(e.get("id")))
            if o is None:
                return
            try:
                sm = float(e.get("size_matched") or 0)
            except (TypeError, ValueError):
                sm = 0.0
            if sm > o["matched"] + 1e-9:
                self._live_matched(o, sm)
            if e.get("type") == "CANCELLATION" and o["status"] in LIVE_ACTIVE:
                o["status"] = "done"

    def _lwin_of(self, slug, start, end):
        w = self.lwin.get(slug)
        if w is None:
            w = self.lwin[slug] = {"slug": slug, "start": start, "end": end, "up_sh": 0.0, "up_cost": 0.0,
                                   "down_sh": 0.0, "down_cost": 0.0, "fills": 0, "winner": None, "pnl": None,
                                   "official": False}
        return w

    def _live_matched(self, o, total, force=False):
        d = total - o["matched"]
        if abs(d) < 1e-9 or (d < 0 and not force):
            return
        o["matched"] = total
        w = self._lwin_of(o["slug"], o["start"], o["end"])
        w[o["side"] + "_sh"] += d
        w[o["side"] + "_cost"] += d * o["price"]
        if d > 0:
            w["fills"] += 1
            self.lstat["fills"] += 1
            self.lstat["fill_shares"] += d
        if total >= o["size"] - 1e-6 and o["status"] in ("live", "cancelling"):
            o["status"] = "done"
        self._live_log("fill", o, shares=round(d, 4))
        m = self.markets.get(o["slug"])
        if m is not None:
            self._requote(m, self.clock() * 1000.0, force=True)

    def _tick(self, now):
        super()._tick(now)
        if not getattr(self, "live_on", False) or now - self._lsettle_ms < 1000:
            return
        self._lsettle_ms = now
        now_s = now / 1000.0
        recs = None
        for w in self.lwin.values():                         # live windows settle on the paper twin's winner
            if w["official"] or now_s < w["end"] + 3:
                continue
            if recs is None:
                recs = {r["slug"]: r for r in self.settled}
            r = recs.get(w["slug"])
            if r is not None and r.get("winner"):
                w["winner"], w["official"] = r["winner"], bool(r.get("official"))
                w["pnl"] = w[r["winner"] + "_sh"] - w["up_cost"] - w["down_cost"]
        if self.gw is None:
            return
        for m in list(self.markets.values()):                # 2 s after a window ends, cancel whatever of ours is open
            if (m.__dict__.get("lv_swept") or now_s < m.end + 2 or not getattr(m, "condition", None)
                    or not any(o["slug"] == m.slug for o in self.lorders.values())):
                continue
            m.lv_swept = True
            self._loop.run_in_executor(self.gw.pool, self.gw.cancel_market, m.condition).add_done_callback(
                lambda f, slug=m.slug: self._live_swept(f, slug))

    def _live_swept(self, f, slug):
        try:
            n = len(f.result()["canceled"])
        except Exception as e:
            self._err(f"live sweep {slug}: {type(e).__name__}: {e}"[:200])
            return
        if n:
            self.lstat["sweep_cancelled"] += n
            self._err(f"live sweep {slug}: cancelled {n} order(s) still open after the window ended")

    def _live_log(self, kind, o, shares=None, info=None):
        self.llog.append({"ts": time.time(), "kind": kind, "slug": o["slug"], "side": o["side"], "price": o["price"],
                          "size": o["size"], "shares": shares, "info": info})

    def _load_live(self):
        """Rebuild the live ledger from this bot's filled orders (a restart mid-window keeps its inventory)."""
        rows = db.q("SELECT slug, outcome, price, filled FROM live_orders WHERE origin = ? AND filled > 0", (self.bot_id,))
        for r in rows:
            try:
                start = int(r["slug"].rsplit("-", 1)[1])
                mins = int(r["slug"].split("-")[2].rstrip("m"))
            except (ValueError, IndexError):
                continue
            w = self._lwin_of(r["slug"], start, start + mins * 60)
            w[r["outcome"] + "_sh"] += float(r["filled"])
            w[r["outcome"] + "_cost"] += float(r["filled"]) * float(r["price"])
            w["fills"] += 1

    async def _main(self):
        if self.mode != "live":
            return await super()._main()
        from ...trading.gateway import Gateway
        self.gw = Gateway()
        self.gw.armed = True                                 # mode 'live' is the arming
        self.gw.light = True
        self.gw.on_event = self._on_live_event
        self.gw.start(asyncio.get_running_loop())
        boot = asyncio.ensure_future(self._live_boot())
        hb = asyncio.ensure_future(self._live_heartbeat())
        try:
            await super()._main()
        finally:
            boot.cancel()
            await self._live_shutdown()
            hb.cancel()

    async def _live_heartbeat(self):
        """Polymarket order heartbeats every live_heartbeat_s (serialized, one task): each accepted send keeps the API
        key's timeout registration alive; a mismatch carries the expected ID (another client sent one) and is retried
        with it at once."""
        from polymarket.errors import OrderHeartbeatMismatchError
        loop = asyncio.get_running_loop()
        hb_id = ""
        while True:
            await asyncio.sleep(self.cfg.live_heartbeat_s if self.cfg.live_heartbeat_s > 0 else 5.0)
            if self.cfg.live_heartbeat_s <= 0 or not self.gw.enabled:
                continue
            for _ in range(2):
                try:
                    r = await loop.run_in_executor(self.gw.bg, lambda i=hb_id: self.gw.client.send_order_heartbeat(
                        heartbeat_id=i))
                    hb_id = r.heartbeat_id
                    self.lstat["hb_ok"] += 1
                    break
                except OrderHeartbeatMismatchError as e:
                    hb_id = e.heartbeat_id
                    self.lstat["hb_mismatch"] += 1
                except Exception as e:
                    self.lstat["hb_errors"] += 1
                    self._err(f"live heartbeat: {type(e).__name__}: {e}"[:200])
                    break

    async def _live_boot(self):
        """Wait for the trading client, then cancel any order an earlier run of this bot left open."""
        loop = asyncio.get_running_loop()
        for _ in range(600):
            if self.gw.enabled:
                break
            await asyncio.sleep(0.1)
        if not self.gw.enabled:
            self._err(f"live: trading client not ready: {self.gw.reason}")
            return
        try:
            mine = {r["order_id"] for r in db.q("SELECT order_id FROM live_orders WHERE origin = ?", (self.bot_id,))}
            rows = await loop.run_in_executor(self.gw.pool, lambda: [o.model_dump(mode="json") for o in
                                                                      self.gw.client.list_open_orders().iter_items()])
            ids = [str(o["id"]) for o in rows if str(o["id"]) in mine]
            if ids:
                r = await loop.run_in_executor(self.gw.pool, self.gw.cancel_batch, ids)
                self.lstat["boot_cancelled"] += len(r["canceled"])
        except Exception as e:
            self._err(f"live: cancelling leftovers: {type(e).__name__}: {e}"[:200])
        self._lready = True

    async def _live_shutdown(self):
        """Cancel every live bid before the process ends (UI stop, SIGTERM)."""
        self._lready = False
        ids = [o["oid"] for o in self.lorders.values() if o["oid"] and o["status"] in ("live", "cancelling")]
        if not ids or self.gw is None or not self.gw.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            r = await asyncio.wait_for(loop.run_in_executor(self.gw.cpool, self.gw.cancel_batch, ids), 3.0)
            self.lstat["stop_cancelled"] += len(r["canceled"])
        except Exception as e:
            self._err(f"live: cancel on stop: {type(e).__name__}: {e}"[:200])

    def _build_snapshot(self):
        super()._build_snapshot()
        if not getattr(self, "live_on", False):
            return
        act = [o for o in self.lorders.values() if o["status"] in LIVE_ACTIVE]
        done = [w for w in self.lwin.values() if w["pnl"] is not None]
        self._snapshot["live"] = {
            "kind": "late_fav", "mode": self.mode, "dry": self.dry, "state": self._live_block(),
            "enabled": True if self.dry else bool(self.gw and self.gw.enabled),
            "usdc": self.gw.balance if self.gw else None, "realized": round(self._realized(), 4),
            "windows_done": len(done), "windows_won": sum(1 for w in done if w["pnl"] > 0),
            "committed": round(self._committed(), 2), "budget": self.cfg.live_budget_usd, "size": self.cfg.live_size,
            "loss_limit": self.cfg.live_loss_limit_usd, "paused": self.cfg.live_paused, "counts": dict(self.lstat),
            "resting": [{k: o[k] for k in ("slug", "side", "price", "size", "matched", "status")} for o in act],
            "windows": sorted(self.lwin.values(), key=lambda w: -w["start"])[:30], "log": list(self.llog)[-40:]}

"""Checks the imb_maker port (backend/bots/strategies/imb_maker.py) against the study's reference code and the
cases of docs/bots/bot_sim/BOT_SPEC.md. Offline: no network, no DB.

  .venv\\Scripts\\python -m scripts.check_imb_maker

1. random inputs through docs/bots/bot_sim/strategy.py (Quoter.desired, BinanceGuard) and the port: identical;
2. spec cases A-G, the quoting clock and the guard case through a real ImbMakerBot (books, ticks, orders);
3. the live layer against a fake gateway (no network): batches, own orders left out of the book, guard cancels,
   fills from user-channel events (no double count, FAILED reversed), inventory cap, rejection backoff, kill
   switch, loss limit, end of quoting.
"""
import asyncio
import time
import importlib.util
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.bots import replay
from backend.bots.strategies import imb_maker as im
from backend.bots.strategies.market_maker import Market

ROOT = Path(__file__).resolve().parent.parent
FAILS = []


def check(ok, what):
    if not ok:
        FAILS.append(what)
        print("FAIL", what)


def load_reference():
    spec = importlib.util.spec_from_file_location("ref_strategy", ROOT / "docs/bots/bot_sim/strategy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fuzz(ref, n=20000):
    rnd = random.Random(7)
    for i in range(n):
        kw = {"imb_join": rnd.choice([0.3, 0.4, 0.5, 0.6]), "back_ticks": rnd.choice([1, 2]),
              "imb_low": rnd.choice([0.0, 0.0, 0.15, 0.3]), "back_low": rnd.choice([2, 3]),
              "age_join": rnd.choice([0.0, 0.0, 0.15])}
        rc = ref.QuoterConfig(**kw)
        pc = im.ImbMakerConfig(**kw)
        q = ref.Quoter(rc, 0.0)
        t = rnd.uniform(5000, 274999)
        bb = rnd.randrange(10, 99) * 10
        ba = bb + rnd.choice([10, 10, 10, 20, 30])
        sbb, sba = rnd.choice([0, rnd.uniform(0, 2000)]), rnd.choice([0, rnd.uniform(0, 2000)])
        t_bb, t_ba = t - rnd.uniform(0, 1000), t - rnd.uniform(0, 1000)
        net = rnd.uniform(-600, 600)
        allow = (rnd.random() > 0.2, rnd.random() > 0.2)
        want_ref = q.desired(t, bb, ba, sbb, sba, t_bb, t_ba, net, allow)
        d = im.top_depths(pc, sbb, sba, (t - t_bb) / 1000, (t - t_ba) / 1000)
        want = im.ladders(pc, bb, ba, *d, net, allow)
        check(want == want_ref, f"fuzz desired #{i}: {kw} bb={bb} ba={ba} sizes={sbb:.0f}/{sba:.0f} net={net:.0f}")
        if FAILS:
            return
    for i in range(500):
        g_ref, g = ref.BinanceGuard(0.5, 1000, 1000), im.BinanceGuard(0.5, 1000, 1000)
        t, p = 0.0, 100000.0
        for _ in range(300):
            t += rnd.expovariate(1 / 40)
            p *= 1 + rnd.gauss(0, 0.4e-4)
            a, b = g_ref.on_tick(t, p), g.on_tick(t, p)
            check(a == b and g_ref.until == g.until, f"fuzz guard #{i} at t={t:.0f}")
            if FAILS:
                return
    print(f"fuzz: {n} decisions and 500 guard streams identical to strategy.py")
    # max_price: levels above it are skipped, n_levels still counted from the top
    pc = im.ImbMakerConfig(min_price=0.15, max_price=0.85, n_levels=7)
    want = im.ladders(pc, 880, 890, 0, 0, 0.0)
    up = sorted(px for s, px in want if s == 1)
    check(up == [820, 830, 840, 850], f"max_price: Up levels {up}, want 0.82..0.85")
    dn = sorted(1000 - px for s, px in want if s == -1)
    check(dn == [], f"min_price: Down levels {dn}, want none (0.11 is below 0.15)")
    want = im.ladders(pc, 100, 110, 0, 0, 0.0)
    dn = sorted(1000 - px for s, px in want if s == -1)
    check(dn == [830, 840, 850], f"max_price: Down levels {dn}, want 0.83..0.85")
    check(not [px for s, px in want if s == 1], "min_price: no Up level below 0.15")


# ---------------------------------------------------------------- the bot itself
T0 = 1_800_000_000          # a window start (any multiple of 300)


def new_bot():
    b = replay.build("check", "imb_maker")
    m = Market(f"btc-updown-5m-{T0}", 5, T0, {"tokens": {"up": "U", "down": "D"}})
    b.markets[m.slug] = m
    return b, m


def at(b, ms):
    b._t = ms / 1000.0


def book(b, m, ms, bb, ba, sbb, sba):
    """Up bids at bb (size sbb) and below, Down bids at 1 - ba (size sba) and below; the mirrored asks."""
    at(b, ms)
    up_bids = [{"price": f"{bb - k / 100:.2f}", "size": str(sbb if k == 0 else 100)} for k in range(20) if bb - k / 100 > 0.005]
    dn_bids = [{"price": f"{1 - ba - k / 100:.2f}", "size": str(sba if k == 0 else 100)} for k in range(20) if 1 - ba - k / 100 > 0.005]
    mirror = lambda bids: [{"price": f"{1 - float(x['price']):.2f}", "size": x["size"]} for x in bids]
    b._on_book_msg(m, [{"event_type": "book", "asset_id": "U", "bids": up_bids, "asks": mirror(dn_bids)},
                       {"event_type": "book", "asset_id": "D", "bids": dn_bids, "asks": mirror(up_bids)}])


def fut(b, ms, price):
    at(b, ms)
    b._on_binance_fut_msg({"p": str(price)})


def resting(m, side, ms):
    """Resting orders of one token, best (ladder top) first."""
    return sorted(((o.price, o.size) for o in m.orders if o.side == side and o.cancel_ms is None), reverse=True)


def cases():
    t = (T0 + 100) * 1000.0
    up_a = [(round(0.48 - k / 100, 2), float(int(60 / (1 - abs(0.48 - k / 100 - 0.5)) + 1e-9))) for k in range(13)]
    dn_a = [(round(0.51 - k / 100, 2), float(int(60 / (1 - abs(0.51 - k / 100 - 0.5)) + 1e-9))) for k in range(13)]
    up_b = [(round(0.46 - k / 100, 2), float(int(60 / (1 - abs(0.46 - k / 100 - 0.5)) + 1e-9))) for k in range(13)]
    up_f = [(round(0.15 - k / 100, 2), float(int(60 / (1 - abs(0.15 - k / 100 - 0.5)) + 1e-9))) for k in range(6)]
    dn_f = [(round(0.84 - k / 100, 2), float(int(60 / (1 - abs(0.84 - k / 100 - 0.5)) + 1e-9))) for k in range(13)]
    table = {  # case: (bb, ba, size at bb, size at ba, net, Up bids, Down bids)
        "A": (0.48, 0.49, 300, 200, 0, up_a, dn_a),
        "B": (0.48, 0.49, 50, 450, 0, up_b, dn_a),
        "C": (0.48, 0.49, 150, 350, 0, up_b, dn_a),
        "D": (0.48, 0.49, 300, 200, 350, [], dn_a),
        "E": (0.48, 0.49, 300, 200, -350, up_a, []),
        "F": (0.15, 0.16, 500, 500, 0, up_f, dn_f),
        "G": (0.48, 0.49, 0, 0, 0, up_a, dn_a),
    }
    # the spec's endpoints, as a check on the expected ladders themselves
    check(up_a[0] == (0.48, 61) and up_a[-1] == (0.36, 69) and dn_a[0] == (0.51, 60) and dn_a[-1] == (0.39, 67), "table A")
    check(up_b[0] == (0.46, 62) and up_b[-1] == (0.34, 71), "table B")
    check(up_f[0] == (0.15, 92) and up_f[-1] == (0.10, 100) and dn_f[0] == (0.84, 90) and dn_f[-1] == (0.72, 76), "table F")
    for name, (bb, ba, sbb, sba, net, up, dn) in table.items():
        b, m = new_bot()
        for k in range(30):                       # a live, quiet guard feed
            fut(b, t - 6000 + k * 200, 100000.0)
        book(b, m, t - 5000, bb, ba, sbb, sba)
        m.shares = {"up": max(net, 0.0), "down": max(-net, 0.0)}
        at(b, t)
        b._requote(m, t, force=True)
        got_up, got_dn = resting(m, "up", t), resting(m, "down", t)
        check(got_up == up, f"case {name} Up: {got_up[:2]}..{got_up[-1:]} ({len(got_up)})")
        check(got_dn == dn, f"case {name} Down: {got_dn[:2]}..{got_dn[-1:]} ({len(got_dn)})")
    print(f"cases A-G: {sum(1 for f in FAILS if f.startswith('case')) == 0 and 'ok' or 'FAILED'}")

    # clock: nothing before start + 5 s, quotes from there, all cancelled at end - 25 s
    b, m = new_bot()
    s0 = T0 * 1000.0
    for k in range(40):
        fut(b, s0 - 2000 + k * 200, 100000.0)
    book(b, m, s0 + 4000, 0.48, 0.49, 300, 200)
    at(b, s0 + 4999)
    b._requote(m, s0 + 4999, force=True)
    check(not m.orders, "clock: orders before start + 5 s")
    at(b, s0 + 5000)
    b._requote(m, s0 + 5000, force=True)
    check(len(resting(m, "up", 0)) == 13 and len(resting(m, "down", 0)) == 13, "clock: no ladders at start + 5 s")
    e = (T0 + 275) * 1000.0
    for k in range(5):
        fut(b, e - 1000 + k * 200, 100000.0)
    at(b, e - 1)
    b._requote(m, e - 1, force=True)
    check(len(resting(m, "up", 0)) == 13, "clock: ladders gone before end - 25 s")
    at(b, e)
    b._requote(m, e, force=True)
    check(not resting(m, "up", 0) and not resting(m, "down", 0), "clock: orders left at end - 25 s")
    check(all(o.cancel_ms == e for o in m.orders), "clock: cancels not sent at end - 25 s")
    print(f"clock: {'ok' if not any(f.startswith('clock') for f in FAILS) else 'FAILED'}")

    # guard: 100000 at t - 1500 and t - 1200, 99994 at t (-0.6 bps) pulls Up at t until t + 1000; +0.4 bps nothing
    b, m = new_bot()
    book(b, m, t - 5000, 0.48, 0.49, 300, 200)
    fut(b, t - 1500, 100000.0)
    fut(b, t - 1200, 100000.0)
    at(b, t - 1)
    b._requote(m, t - 1, force=True)
    up_ids = {o.id for o in m.orders if o.side == "up"}
    check(len(up_ids) == 13 and len(resting(m, "down", 0)) == 13, "guard: ladders before the move")
    fut(b, t, 99994.0)
    check(all(o.cancel_ms == t for o in m.orders if o.id in up_ids), "guard: Up bids not cancelled at t")
    check(not resting(m, "up", 0) and len(resting(m, "down", 0)) == 13, "guard: Down should keep quoting at t")
    for dt in (500, 999):
        at(b, t + dt)
        b._requote(m, t + dt, force=True)
        check(not resting(m, "up", 0), f"guard: Up back before t + 1000 (t + {dt})")
    at(b, t + 1000)
    b._requote(m, t + 1000, force=True)
    check(len(resting(m, "up", 0)) == 13, "guard: Up not back at t + 1000")
    check(len(resting(m, "down", 0)) == 13 and all(o.cancel_ms is None for o in m.orders if o.side == "down"),
          "guard: Down orders were touched")
    log = m.im["guard"]
    check(len(log) == 1 and log[0][5] == "up" and log[0][7] == 13 and abs(log[0][4] + 0.6) < 1e-6, f"guard log {log}")
    b, m = new_bot()
    book(b, m, t - 5000, 0.48, 0.49, 300, 200)
    fut(b, t - 1500, 100000.0)
    fut(b, t - 1200, 100000.0)
    at(b, t - 1)
    b._requote(m, t - 1, force=True)
    fut(b, t, 100004.0)
    check(len(resting(m, "up", 0)) == 13 and len(resting(m, "down", 0)) == 13, "guard: +0.4 bps pulled something")
    # no guard feed (or a stale one): nothing is quoted
    b, m = new_bot()
    book(b, m, t - 5000, 0.48, 0.49, 300, 200)
    at(b, t)
    b._requote(m, t, force=True)
    check(not m.orders, "guard: quoted without a guard feed")
    print(f"guard: {'ok' if not any(f.startswith('guard') for f in FAILS) else 'FAILED'}")


class FakeGateway:
    """Answers like Gateway.post_limit_batch / cancel_batch, records every call."""
    enabled, reason, balance, reconnects = True, "", 300.0, 0

    def __init__(self):
        self.pool = self.bg = self.cpool = ThreadPoolExecutor(1)
        self.posts, self.cancels, self.reject, self.n, self.errors = [], [], set(), 0, []
        self.fail_cancels = 0
        self.open = []

    def open_orders_in(self, condition_id):
        return list(self.open)

    def _check_size(self, usd):
        pass

    def post_limit_batch(self, orders, origin):
        out = []
        for _slug, side, _tok, px, _sz in orders:
            self.n += 1
            if (side, px) in self.reject:
                out.append({"ok": False, "order_id": None, "error": "crosses", "code": "post_only_would_cross"})
            else:
                out.append({"ok": True, "order_id": f"o{self.n}", "error": None, "code": None, "status": "live"})
        self.posts.append(list(orders))
        return {"orders": out, "post_ms": 20.0}

    def cancel_batch(self, ids):
        if self.fail_cancels:
            self.fail_cancels -= 1
            raise RuntimeError("429 Too Many Requests")
        self.cancels.append(list(ids))
        return {"canceled": list(ids), "not_canceled": {}, "ms": 20.0}


def live():
    loop = asyncio.new_event_loop()
    flush = lambda: loop.run_until_complete(asyncio.sleep(0.05))
    b, m = new_bot()
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "B": 5.0, "inv_cap": 25.0})
    gw = b.gw = FakeGateway()
    b._loop, b.live_on, b._lready = loop, True, True
    t = (T0 + 100) * 1000.0
    for k in range(30):
        fut(b, t - 6000 + k * 200, 100000.0)
    book(b, m, t - 5000, 0.48, 0.49, 300, 200)
    act = lambda side=None: [o for o in b.lorders.values() if o["status"] == "live" and (side is None or o["side"] == side)]
    at(b, t)
    b._requote(m, t, force=True)
    flush()
    sizes = [len(p) for p in gw.posts]
    up = sorted((o["price"], o["size"]) for o in act("up"))
    check(sizes == [15, 11], f"live: batches {sizes}, want [15, 11]")
    check(len(act("up")) == 13 and len(act("down")) == 13, f"live: {len(act('up'))}/{len(act('down'))} live orders")
    check(up[-1] == (0.48, 5.0) and up[0] == (0.36, 5.0), f"live: Up ladder {up[:1]}..{up[-1:]}")
    check(sorted(o["price"] for o in act("up")) == sorted(p for p, _ in resting(m, "up", 0)), "live: live != paper prices")
    # our own orders now show in the book: the decision must not change (they are left out of the share)
    n_post, n_cancel = len(gw.posts), len(gw.cancels)
    for o in b.lorders.values():
        bk = m.books[o["side"]]["bids"]
        bk[o["price"]] = bk.get(o["price"], 0.0) + o["size"]
    m.books["up"]["bids"][0.49] = 5.0                  # a level of ours alone above the best: others' best stays 0.48
    b.lorders[999] = {**act("up")[0], "lid": 999, "price": 0.49, "oid": "mine"}
    _lv = m.__dict__["lv"]
    _lv["act"].append(999)
    at(b, t + 10)
    b._requote(m, t + 10, force=True)
    flush()
    check(len(gw.posts) == n_post, f"live: own orders in the book caused new posts {gw.posts[n_post:]}")
    check(gw.cancels[n_cancel:] == [["mine"]], f"live: only the stray 0.49 order should be cancelled: {gw.cancels[n_cancel:]}")
    m.books["up"]["bids"].pop(0.49)
    # guard: Up live orders cancelled in one batch, Down untouched
    n_cancel = len(gw.cancels)
    up_ids = {o["oid"] for o in act("up")}
    fut(b, t + 20, 99994.0)
    flush()
    check(len(gw.cancels) == n_cancel + 1 and set(gw.cancels[-1]) == up_ids, "live: guard did not cancel all Up orders at once")
    check(not act("up") and len(act("down")) == 13, f"live: after guard {len(act('up'))}/{len(act('down'))}")
    # a fill: trade event (MATCHED), the order event repeating it, then the trade FAILED
    o = sorted(act("down"), key=lambda o: -o["price"])[0]
    trade = {"event_type": "trade", "id": "tr1", "status": "MATCHED",
             "maker_orders": [{"order_id": o["oid"], "matched_amount": "3", "price": str(o["price"])},
                              {"order_id": "someone-else", "matched_amount": "50", "price": "0.5"}]}
    at(b, t + 30)
    b._on_live_event(trade)
    b._on_live_event({**trade, "status": "MINED"})
    b._on_live_event({"event_type": "order", "id": o["oid"], "type": "UPDATE", "size_matched": "3"})
    w = b.live[m.slug]
    check(abs(w["down_sh"] - 3) < 1e-9 and abs(w["down_cost"] - 3 * o["price"]) < 1e-9, f"live: fill counted {w}")
    b._on_live_event({**trade, "status": "FAILED"})
    check(abs(w["down_sh"]) < 1e-9, f"live: FAILED trade not reversed {w['down_sh']}")
    # inventory cap: 30 Down shares -> net -30 < -25: Down ladder cancelled
    b._on_live_event({**trade, "id": "tr2", "maker_orders": [{"order_id": o["oid"], "matched_amount": "5"}]})
    o2 = sorted(act("down"), key=lambda o: -o["price"])[0]
    w["down_sh"] += 25
    w["down_cost"] += 25 * 0.5
    at(b, t + 40)
    b._requote(m, t + 40, force=True)
    flush()
    check(not act("down"), f"live: Down still quoted at net {w['up_sh'] - w['down_sh']}")
    # (orders acked only now were already unwanted while in flight and keep that reason)
    check(all(x["why"] == "cap" for x in b.lorders.values() if x["side"] == "down" and x["csent"] and x["csent"] >= t + 40
              and x["ack"] is not None and x["ack"] < t + 40), "live: cap cancels not tagged 'cap'")
    w["down_sh"], w["down_cost"] = 0.0, 0.0
    # rejection backoff: Up comes back after the guard, its top price is refused, not re-sent for 300 ms
    gw.reject.add(("up", 0.48))
    at(b, t + 1100)
    b._requote(m, t + 1100, force=True)
    flush()
    tops = lambda: sum(1 for p in gw.posts for x in p if x[1] == "up" and x[3] == 0.48)
    n0 = tops()
    check(n0 >= 1 and len(act("up")) == 12, f"live: Up after guard {len(act('up'))} live, top sent {n0}x")
    at(b, t + 1200)
    b._requote(m, t + 1200, force=True)
    flush()
    check(tops() == n0, "live: rejected price re-sent inside its backoff")
    at(b, t + 1450)
    b._requote(m, t + 1450, force=True)
    flush()
    check(tops() == n0 + 1, "live: rejected price not retried after its backoff")
    gw.reject.clear()                                  # (the retry at +1450 was refused again: backoff to +1750)
    # kill switch, then the loss limit
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "live_paused": True})
    at(b, t + 1500)
    b._requote(m, t + 1500, force=True)
    flush()
    check(not act(), f"live: kill switch left {len(act())} orders")
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "live_paused": False})
    at(b, t + 1760)
    b._requote(m, t + 1760, force=True)
    flush()
    check(len(act()) == 26, f"live: not back after the kill switch ({len(act())})")
    b.live["btc-updown-5m-1"] = {"slug": "x", "win": 5, "start": 0, "end": 1, "up_sh": 0, "down_sh": 0, "up_cost": 0,
                                 "down_cost": 0, "pnl": -61.0, "rebate": 0, "fills": 1, "winner": "up", "merge": None}
    b._tick(t + 1770)
    at(b, t + 1780)
    b._requote(m, t + 1780, force=True)
    flush()
    check(not act() and b._live_block(None) == "loss_limit", f"live: loss limit left {len(act())} orders")
    b.live.pop("btc-updown-5m-1")
    b._tick(t + 1790)
    # reconcile: an order the exchange has but we lost is adopted (its fill counted, then managed); a tracked one
    # the exchange no longer has is dropped
    at(b, t + 1791)
    b._requote(m, t + 1791, force=True)
    flush()
    tok = {"up": "U", "down": "D"}
    gw.open = [{"id": o["oid"], "asset_id": tok[o["side"]], "side": "BUY", "price": str(o["price"]),
                "original_size": str(o["size"]), "size_matched": "0"} for o in act()]
    gone = gw.open.pop()["id"]
    gw.open.append({"id": "lost1", "asset_id": "D", "side": "BUY", "price": "0.3", "original_size": "6", "size_matched": "2"})
    dn0 = b.live[m.slug]["down_sh"]
    at(b, t + 1792)
    b._live_reconcile(m, t + 5000)
    flush()
    flush()
    check(b.lstat["adopted"] == 1 and abs(b.live[m.slug]["down_sh"] - dn0 - 2) < 1e-9, "live: lost order not adopted / fill missed")
    check(any("lost1" in c for c in gw.cancels), "live: adopted unwanted order not cancelled")
    check(b.loid[gone]["status"] == "done" and b.lstat["vanished"] >= 1,    # (orders still cancelling count too)
          f"live: order gone from the exchange not dropped ({b.loid[gone]['status']}, {b.lstat['vanished']})")
    gw.open = []
    # spend cap: once reached, only the lighter side is bid
    w = b.live[m.slug]
    saved = dict(w)
    w.update(up_sh=0.0, down_sh=10.0, up_cost=0.0, down_cost=5.0)
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "spend_cap_usd": 1.0})
    b._otok = 45.0                                     # (virtual time barely moves: refill the order budget)
    at(b, t + 1794)
    b._requote(m, t + 1794, force=True)
    flush()
    check(not act("down") and len(act("up")) >= 12, f"live: spend cap: {len(act('up'))} Up / {len(act('down'))} Down")
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "spend_cap_usd": 0.0})
    w.update(saved)
    # end of quoting: everything cancelled at end - 25 s
    at(b, t + 1800)
    b._requote(m, t + 1800, force=True)
    flush()
    e = (T0 + 275) * 1000.0
    for k in range(5):
        fut(b, e - 1000 + k * 200, 100000.0)
    flush()                                            # (the rise back to 100000 has pulled Down by now)
    n_live = len(act())
    gw.fail_cancels = 1                                # the first cancel request fails (e.g. a 429)
    at(b, e)
    b._requote(m, e, force=True)
    flush()
    check(n_live > 0 and len(act()) == n_live and b.lstat["cancel_errors"] == 1,
          f"live: failed cancel: {len(act())} of {n_live} live, {b.lstat['cancel_errors']} errors")
    n_cancel = len(gw.cancels)
    at(b, e + 100)
    b._requote(m, e + 100, force=True)
    flush()
    check(len(gw.cancels) == n_cancel and len(act()) == n_live, "live: cancel retried inside its 200 ms pause")
    at(b, e + 250)
    b._requote(m, e + 250, force=True)
    flush()
    check(not act() and not any(x["status"] == "sending" for x in b.lorders.values()), "live: orders left at end - 25 s")
    loop.close()
    print(f"live layer: {'ok' if not any(f.startswith('live') for f in FAILS) else 'FAILED'}")


class FakeFast:
    """Answers like trading/fastorders.FastOrders: local ids f1, f2, ...; posts can be slow or rejected."""

    def __init__(self):
        self.pool, self.cpool = ThreadPoolExecutor(4), ThreadPoolExecutor(4)
        self.n, self.posts, self.cancels, self.verified, self.delay = 0, [], [], True, 0.0
        self.cancelled, self.transport = set(), 0       # transport: the next n posts lose their connection

    def sign(self, token, price, size, post_only=True):
        self.n += 1
        return (token, price, size), f"f{self.n}", 0.5

    def _post(self, signed, oid):
        time.sleep(self.delay)
        self.posts.append((oid, signed))
        if self.transport:
            self.transport -= 1
            return {"ok": False, "order_id": oid, "local_id": oid, "error": "TransportError: Server disconnected",
                    "code": "transport", "status": "rejected", "post_ms": 5.0}
        if oid in self.cancelled:
            return {"ok": False, "order_id": oid, "local_id": oid, "error": f"order {oid} is invalid. Already canceled.",
                    "code": None, "status": "rejected", "post_ms": 20.0}
        return {"ok": True, "order_id": oid, "local_id": oid, "error": None, "code": None, "status": "live",
                "post_ms": 20.0}

    def _cancel(self, ids):
        self.cancels.append(list(ids))
        self.cancelled |= set(ids)
        return {"canceled": list(ids), "not_canceled": {}, "error": None, "ms": 20.0}


def live_fast():
    """The fast order path: one post per order, ids known at signing, cancels sent while posts are in flight."""
    loop = asyncio.new_event_loop()
    flush = lambda d=0.05: loop.run_until_complete(asyncio.sleep(d))
    b, m = new_bot()
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "B": 5.0, "inv_cap": 25.0})
    b.gw, fo = FakeGateway(), FakeFast()
    b.fo = fo
    b._loop, b.live_on, b._lready = loop, True, True
    t = (T0 + 100) * 1000.0
    for k in range(30):
        fut(b, t - 6000 + k * 200, 100000.0)
    book(b, m, t - 5000, 0.48, 0.49, 300, 200)
    live = lambda: [o for o in b.lorders.values() if o["status"] == "live"]
    at(b, t)
    b._requote(m, t, force=True)
    flush()
    check(len(fo.posts) == 26 and len(live()) == 26 and not b.gw.posts, f"fast: {len(fo.posts)} single posts, {len(live())} live")
    check(all(o["oid"] == f"f{i + 1}" for i, o in enumerate(sorted(live(), key=lambda o: o["lid"]))), "fast: ids = local ids")
    # slow posts: a guard pull while they are in flight cancels them at once, by id, before any ack
    fo.delay = 0.3
    book(b, m, t + 100, 0.50, 0.51, 300, 200)          # the market moves up: new levels are posted (slowly)
    at(b, t + 100)
    b._requote(m, t + 100, force=True)
    flush(0.02)
    sending = [o for o in b.lorders.values() if o["status"] == "sending"]
    n_c = len(fo.cancels)
    for k in range(3):                                 # Binance futures falls 0.6 bps: pull every Up bid
        fut(b, t + 150 + k, 100000.0 - 6.0 * (k + 1))
    at(b, t + 160)
    b._requote(m, t + 160, force=True)
    flush(0.02)
    up_sending = [o for o in sending if o["side"] == "up"]
    flush(0.6)
    check(up_sending and len(fo.cancels) > n_c and all(o["csent"] is not None and o["csent"] < o["ack"] for o in up_sending),
          f"fast: {len(up_sending)} Up orders in flight cancelled before their ack")
    check(all(o["status"] == "done" for o in up_sending), "fast: cancelled in-flight orders end done")
    check(b.lstat["cancelled_in_flight"] >= 1 or all(o["code"] != "rejected" for o in up_sending),
          "fast: post that lost the race to its cancel counted as cancelled_in_flight")
    check(all(len(c) == 1 for c in fo.cancels), "fast: one id per cancel request")
    fo.verified = False                                # a wrong local id: the fast path switches itself off
    b.fo._post = lambda signed, oid: {"ok": True, "order_id": "server-id", "local_id": oid, "error": None,
                                      "code": None, "status": "live", "post_ms": 20.0}
    fo.delay = 0.0
    book(b, m, t + 3000, 0.46, 0.47, 300, 200)
    at(b, t + 3000)
    b._requote(m, t + 3000, force=True)
    flush()
    check(b.fo is None, "fast: off after an id mismatch")
    loop.close()
    print(f"fast path: {'ok' if not any(f.startswith('fast') for f in FAILS) else 'FAILED'}")


def live_fixes():
    """2026-10-27 fixes (imb-maker-10's first live window): re-priced orders are cancelled only once answered (no
    cancel in flight for a requote: it made Polymarket hold the post's answer), urgent cancels still in flight; a post
    lost in transit stays managed; a 'rejected' order the exchange has is taken back; the in-flight cap."""
    loop = asyncio.new_event_loop()
    flush = lambda d=0.05: loop.run_until_complete(asyncio.sleep(d))
    b, m = new_bot()
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "B": 5.0, "inv_cap": 25.0, "n_levels": 1})
    b.gw, fo = FakeGateway(), FakeFast()
    b.fo = fo
    b._loop, b.live_on, b._lready = loop, True, True
    t = (T0 + 100) * 1000.0
    for k in range(30):
        fut(b, t - 6000 + k * 200, 100000.0)
    side_of = lambda side, st=("sending", "live", "cancelling"): [o for o in b.lorders.values()
                                                                 if o["side"] == side and o["status"] in st]
    # 1. a requote while the posts are in flight: nothing cancelled in flight, no second order on the side
    fo.delay = 0.3
    book(b, m, t, 0.48, 0.49, 300, 200)
    b._requote(m, t, force=True)
    flush(0.02)
    first = {o["lid"]: o for o in b.lorders.values()}
    check(len(first) == 2 and all(o["status"] == "sending" for o in first.values()), f"fix: {len(first)} first posts")
    n_post, n_c = len(fo.posts), len(fo.cancels)
    book(b, m, t + 20, 0.50, 0.51, 300, 200)           # the market moves while both posts are in flight
    b._requote(m, t + 20, force=True)
    flush(0.02)
    check(len(fo.cancels) == n_c, f"fix: requote cancelled in flight ({len(fo.cancels) - n_c} cancels)")
    check(all(o["want_cancel"] == "requote" for o in first.values()), "fix: in-flight orders not marked for a cancel")
    check(len(b.lorders) == 2, f"fix: new orders placed while the old ones fly ({len(b.lorders)} orders)")
    at(b, t + 400)
    flush(0.5)                                          # answers come back: cancelled now, then the new prices posted
    flush(0.5)
    check(all(o["csent"] is not None and o["csent"] >= o["ack"] for o in first.values()),
          "fix: re-priced orders not cancelled after their answer")
    up = side_of("up", ("sending", "live"))
    check(len(up) == 1 and up[0]["price"] == 0.50, f"fix: Up after the requote {[(o['price'], o['status']) for o in up]}")
    # 2. flicker: moved and back before the answer: the order simply stays
    book(b, m, t + 1000, 0.52, 0.53, 300, 200)
    b._requote(m, t + 1000, force=True)
    flush(0.02)
    fly = [o for o in b.lorders.values() if o["status"] == "sending" and o["side"] == "up"]
    book(b, m, t + 1010, 0.51, 0.52, 300, 200)
    b._requote(m, t + 1010, force=True)
    book(b, m, t + 1020, 0.52, 0.53, 300, 200)
    b._requote(m, t + 1020, force=True)
    n_c = len(fo.cancels)
    at(b, t + 1400)
    flush(0.5)
    check(len(fly) == 1 and fly[0]["status"] == "live" and fly[0]["want_cancel"] is None and fly[0]["csent"] is None,
          f"fix: flickered order {[(o['price'], o['status'], o['want_cancel']) for o in fly]}")
    # 3. the guard: a deferred (requote) order is now cancelled in flight
    book(b, m, t + 1500, 0.54, 0.55, 300, 200)
    b._requote(m, t + 1500, force=True)
    flush(0.02)
    book(b, m, t + 1510, 0.53, 0.54, 300, 200)         # new Up post in flight gets a deferred requote cancel
    b._requote(m, t + 1510, force=True)
    fly = [o for o in b.lorders.values() if o["status"] == "sending" and o["side"] == "up"]
    for k in range(3):                                  # Binance futures falls: pull every Up bid
        fut(b, t + 1520 + k, 100000.0 - 6.0 * (k + 1))
    flush(0.02)
    check(fly and all(o["csent"] is not None and o["ack"] is None for o in fly),     # cancel sent, post not answered
          f"fix: guard did not cancel a deferred order in flight {[(o['status'], o['csent'], o['ack']) for o in fly]}")
    at(b, t + 3000)
    flush(0.5)
    for k in range(5):
        fut(b, t + 3000 + k * 200, 100000.0)
    fo.delay = 0.0
    # 4. a post lost in transit: tracked as live / unconfirmed; dropped when the exchange does not have it
    for o in list(b.lorders.values()):
        if o["status"] in ("live", "sending", "cancelling"):
            o["status"] = "done"
    fo.transport = 1
    book(b, m, t + 4000, 0.46, 0.47, 300, 200)
    b._requote(m, t + 4000, force=True)
    flush()
    lost = [o for o in b.lorders.values() if o["code"] == "unconfirmed"]
    check(len(lost) == 1 and lost[0]["status"] == "live" and b.lstat["post_unconfirmed"] == 1,
          f"fix: lost post {[(o['status'], o['code']) for o in lost]}")
    b.gw.open = []
    at(b, t + 4400)
    b._live_reconcile(m, t + 4400)
    flush()
    flush()
    check(lost[0]["status"] == "done", f"fix: unconfirmed order the exchange lacks not dropped ({lost[0]['status']})")
    check(len(side_of(lost[0]["side"], ("sending", "live"))) == 1, "fix: lost order not replaced")
    # ... and cancelled by its id once it is unwanted (with the fake book, which lacks our orders, the side it hit may
    # re-price at once: as soon as it counts as live its size is left out of the queue share)
    fo.transport = 1
    n_c = len(fo.cancels)
    book(b, m, t + 5000, 0.44, 0.45, 300, 200)
    b._requote(m, t + 5000, force=True)
    flush()
    book(b, m, t + 5100, 0.42, 0.43, 300, 200)
    b._requote(m, t + 5100, force=True)
    flush()
    lost = [o for o in b.lorders.values() if o["code"] == "unconfirmed" and o["sent"] >= t + 5000]
    check(len(lost) == 1 and any(lost[0]["oid"] in c for c in fo.cancels[n_c:]) and lost[0]["status"] == "done",
          f"fix: unwanted unconfirmed order not cancelled {[(o['oid'], o['status'], o['csent']) for o in lost]}")
    # 5. an order we marked rejected that the exchange has open: managed again, its fill counted
    o = side_of("down", ("live",))[0]
    o["status"], o["code"] = "rejected", "exception"
    b.gw.open = [{"id": o["oid"], "asset_id": "D", "side": "BUY", "price": str(o["price"]),
                  "original_size": str(o["size"]), "size_matched": "2"}]
    dn0 = b.live.get(m.slug, {}).get("down_sh", 0.0)
    at(b, t + 5200)
    b._live_reconcile(m, t + 5200)
    flush()
    flush()
    check(o["status"] in ("live", "cancelling", "done") and b.lstat["readopted"] == 1
          and abs(b.live[m.slug]["down_sh"] - dn0 - 2) < 1e-9, f"fix: rejected-but-open order {o['status']} {b.lstat['readopted']}")
    b.gw.open = []
    # 6. in-flight cap: one post awaiting its answer at a time
    for x in list(b.lorders.values()):
        x["status"] = "done"
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "live_max_inflight": 1})
    fo.delay = 0.3
    book(b, m, t + 6000, 0.40, 0.41, 300, 200)
    b._requote(m, t + 6000, force=True)
    flush(0.02)
    check(sum(1 for x in b.lorders.values() if x["status"] == "sending") == 1, "fix: in-flight cap not applied")
    flush(0.7)
    loop.close()
    print(f"live fixes: {'ok' if not any(f.startswith('fix') for f in FAILS) else 'FAILED'}")


def spend_and_stall():
    """spend_cap_hard (lighter side until flat or flipped, then nothing) and live_stall_ms (no new posts while the
    market's feed is silent or a post waits that long; cancels still go)."""
    b, m = new_bot()
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "spend_cap_usd": 6.0})
    st = {}
    check(b._spend_allow(st, 5.0, 5.0) == (True, True), "spend: below the cap both sides")
    check(b._spend_allow(st, 7.0, 5.0) == (False, True) and b._spend_allow(st, 9.0, -1.0) == (True, False),
          "spend: plain rule = lighter side, also after a flip")
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "spend_cap_hard": True})
    st = {}
    seq = [b._spend_allow(st, c, n) for c, n in ((7.0, 5.0), (7.0, 5.0), (9.5, 0.0), (9.5, 0.0), (12.0, -5.0))]
    check(seq == [(False, True), (False, True), (False, False), (False, False), (False, False)],
          f"spend: hard cap long Up -> Down only -> flat -> nothing {seq}")
    st = {}
    seq = [b._spend_allow(st, c, n) for c, n in ((7.0, -5.0), (9.9, 1.0), (9.9, -4.0))]
    check(seq == [(True, False), (False, False), (False, False)], f"spend: hard cap flip ends it {seq}")
    check(b._spend_allow({}, 7.0, 0.0) == (False, False), "spend: hard cap reached flat: nothing")
    st = {}                                              # 03:40 live window: a partial fill left 0.008 sh of dust
    seq = [b._spend_allow(st, c, n) for c, n in ((8.85, -5.0076), (11.4, -0.0076))]
    check(seq == [(True, False), (False, False)], f"spend: hard cap under a share apart is flat {seq}")
    # stall gate on the live layer
    loop = asyncio.new_event_loop()
    flush = lambda d=0.05: loop.run_until_complete(asyncio.sleep(d))
    b, m = new_bot()
    b.cfg = b.CONFIG.from_dict({**b.cfg.to_dict(), "B": 5.0, "inv_cap": 25.0, "n_levels": 1, "live_stall_ms": 300.0})
    b.gw, fo = FakeGateway(), FakeFast()
    b.fo = fo
    b._loop, b.live_on, b._lready = loop, True, True
    t = (T0 + 100) * 1000.0
    for k in range(30):
        fut(b, t - 6000 + k * 200, 100000.0)
    book(b, m, t, 0.48, 0.49, 300, 200)
    b._requote(m, t, force=True)
    flush()
    check(len(fo.posts) == 2, f"stall: normal quoting {len(fo.posts)} posts")
    n_p, n_c = len(fo.posts), len(fo.cancels)
    m.books["up"]["bids"][0.50] = 50.0                   # the book changed, but its feed has been silent 0.5 s
    m.books["down"]["bids"].pop(0.51, None)
    m.books["down"]["bids"][0.49] = 50.0
    at(b, t + 500)
    b._requote(m, t + 500, force=True)
    flush()
    check(len(fo.posts) == n_p and len(fo.cancels) > n_c and b.lstat["stalls"] == 1,
          f"stall: silent feed: posts {len(fo.posts) - n_p}, cancels {len(fo.cancels) - n_c}, stalls {b.lstat['stalls']}")
    book(b, m, t + 600, 0.50, 0.51, 300, 200)           # the feed is back
    flush()
    check(len(fo.posts) > n_p, "stall: no posts after the feed came back")
    fo.delay = 0.5                                       # a post that waits > 300 ms also counts as a stall
    n_p = len(fo.posts)
    book(b, m, t + 700, 0.52, 0.53, 300, 200)
    flush(0.02)
    sent0 = sum(1 for x in b.lorders.values() if x["status"] == "sending")
    at(b, t + 1100)
    book(b, m, t + 1100, 0.54, 0.55, 300, 200)
    flush(0.02)
    sent1 = sum(1 for x in b.lorders.values() if x["status"] == "sending")
    check(sent0 >= 1 and sent1 == sent0, f"stall: placed behind a post waiting 400 ms ({sent0} -> {sent1} in flight)")
    flush(0.6)
    loop.close()
    print(f"spend cap / stall: {'ok' if not any(f.startswith(('spend', 'stall')) for f in FAILS) else 'FAILED'}")


def exposure():
    """exposure_cap: a side's resting size + net toward it stays within inv_cap, levels from the top."""
    from backend.bots.strategies.imb_maker import ImbMakerConfig, ladders
    c = ImbMakerConfig(B=5.0, inv_cap=25.0, exposure_cap=True)
    w = ladders(c, 480, 490, 2, 2, 0.0)
    up = sorted((px for s, px in w if s == 1), reverse=True)
    dn = sorted(px for s, px in w if s == -1)
    check(sum(w[(1, p)] for p in up) <= 25 and up[0] == 460 and up == [460 - 10 * k for k in range(len(up))],
          f"exposure: Up ladder {up} from the top, {sum(w[(1, p)] for p in up)} sh <= 25")
    check(sum(w[(-1, p)] for p in dn) <= 25 and dn[0] == 510, f"exposure: Down ladder {dn}")
    w = ladders(c, 480, 490, 2, 2, 18.0)               # long 18 Up: room 7 on Up, 43 on Down
    check(sum(sz for (s, _), sz in w.items() if s == 1) <= 7 and sum(sz for (s, _), sz in w.items() if s == -1) <= 43,
          "exposure: room shrinks with net")
    print(f"exposure cap: {'ok' if not any(f.startswith('exposure') for f in FAILS) else 'FAILED'}")


def main():
    fuzz(load_reference())
    cases()
    live()
    live_fast()
    live_fixes()
    spend_and_stall()
    exposure()
    if FAILS:
        sys.exit(f"{len(FAILS)} check(s) failed")
    print("all checks passed")


if __name__ == "__main__":
    main()

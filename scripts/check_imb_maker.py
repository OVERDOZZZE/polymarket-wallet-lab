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
        self.pool = self.bg = ThreadPoolExecutor(1)
        self.posts, self.cancels, self.reject, self.n, self.errors = [], [], set(), 0, []
        self.fail_cancels = 0
        self.open = []

    def open_orders_in(self, condition_id):
        return list(self.open)

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


def main():
    fuzz(load_reference())
    cases()
    live()
    if FAILS:
        sys.exit(f"{len(FAILS)} check(s) failed")
    print("all checks passed")


if __name__ == "__main__":
    main()

"""Checks the imb_maker port (backend/bots/strategies/imb_maker.py) against the study's reference code and the
cases of docs/bots/bot_sim/BOT_SPEC.md. Offline: no network, no DB.

  .venv\\Scripts\\python -m scripts.check_imb_maker

1. random inputs through docs/bots/bot_sim/strategy.py (Quoter.desired, BinanceGuard) and the port: identical;
2. spec cases A-G, the quoting clock and the guard case through a real ImbMakerBot (books, ticks, orders).
"""
import importlib.util
import random
import sys
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


def main():
    fuzz(load_reference())
    cases()
    if FAILS:
        sys.exit(f"{len(FAILS)} check(s) failed")
    print("all checks passed")


if __name__ == "__main__":
    main()

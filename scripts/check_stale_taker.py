"""Offline check of the stale_taker strategy (python -m scripts.check_stale_taker): entry rules on a hand-made
window (trigger side, limit = ask + 2 ticks, price band, cooldown, per-window cap, never live), the latency
draw, and a paper fill against the book at the match time. No DB, no sockets."""
import random
import sys

from backend.bots.replay import build
from backend.bots.strategies.market_maker import Market
from backend.bots.strategies.stale_taker import LATENCY_Q, draw_latency

FAILS = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


def window(b, t0, up_ask=0.50, down_ask=0.51):
    m = Market("btc-updown-5m-%d" % t0, 5, t0, {"tokens": {"up": "U", "down": "D"}, "tick": 0.01, "min_size": 5,
                                                 "fee_rate": 0.07})
    m.books["up"]["asks"] = {up_ask: 500.0, round(up_ask + 0.01, 2): 500.0}
    m.books["down"]["asks"] = {down_ask: 500.0}
    m.book_ok, m.last_book_ms = True, b._t
    b.markets[m.slug] = m
    return m


def main():
    b = build("check", "stale_taker", {"spike_bps": 1.0})
    t0 = 1_800_000_000
    b._t = (t0 + 60) * 1000.0
    m = window(b, t0)
    b.cash = 1000.0
    check("paper only", b.mode == "paper" and b.gw is None)
    sig = {}
    why = b._entry(m, "up", 1.2e-4, b._t, sig)
    check("order placed", why is None and len(b.pending) == 1)
    o = b.pending[0]
    check("limit = ask + 2 ticks", abs(o.limit - 0.52) < 1e-9)
    check("10 shares at the limit", abs(o.budget - 5.20) < 1e-9)
    lat = o.match_ts - b._corr(b._t)
    check("latency inside the real range", LATENCY_Q[0] - 1 <= lat <= LATENCY_Q[-1] + 1)
    check("cooldown", b._entry(m, "up", 1.2e-4, b._t + 500, {}) == "cooldown")
    m2 = window(b, t0 + 300, up_ask=0.91)
    check("price cap (0.91 + 2 ticks > 0.92)", b._entry(m2, "up", 1.2e-4, b._t, {}) == "price_cap")
    res = []
    for k in range(9):
        m.last_book_ms = b._t + 2000 * (k + 1)
        res.append(b._entry(m, "down", -1.2e-4, b._t + 2000 * (k + 1), {}))
    m.last_book_ms = b._t + 30000
    check("max 10 per window", res == [None] * 9 and b._entry(m, "down", -1.2e-4, b._t + 30000, {}) == "max_entries")
    # fill: the first order executes against the book we hold at its match time
    b.pending = [o]
    b._execute(o, b._t + lat)
    check("filled at the ask", m.shares["up"] > 9.5 and abs(m.cost["up"] - o.budget) < 0.06)
    rng = random.Random(7)
    xs = sorted(draw_latency(rng) for _ in range(20000))
    check("latency median ~713 ms", 650 < xs[10000] < 780)
    print("all checks passed" if not FAILS else f"{len(FAILS)} FAILED")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

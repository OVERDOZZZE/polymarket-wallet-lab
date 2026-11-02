"""Resting-order probe (server, REAL orders that cannot fill): do our resting orders survive, or does the exchange
cancel them after ~16 s as it did on 2026-10-27 (memory order-heartbeat-cancels)?

    .venv/bin/python -m scripts.resting_probe [hold_s]        (from ~/app; default hold 60 s)

Posts 3 post-only BUYs of 5 Up shares at 0.01 / 0.02 / 0.03 (max cost $0.30 if all filled) on a 5m window that opens
>= 2 min later (its book sits near 0.50, so nothing can fill), polls each order's status every second, cancels the
survivors after hold_s, then sweeps: any of THESE order ids still open is cancelled. Prints statuses and timings
only - never keys or wallet data. Output: data/probe/resting-<utc>.json.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from backend.polymarket.markets import fetch_market  # noqa: E402
from backend.trading.gateway import Gateway, _api_error  # noqa: E402

PRICES, SIZE = ("0.01", "0.02", "0.03"), "5"


def main():
    hold = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    g = Gateway()
    g._init()
    if not g.enabled:
        sys.exit(f"trading client not available: {g.reason}")
    c = g.client
    print("closed-only mode:", c.get_closed_only_mode())
    now = time.time()
    start = int(now) // 300 * 300 + 300
    if start - now < hold + 60:
        start += 300
    m = fetch_market(f"btc-updown-5m-{start}")
    tok = m["tokens"]["up"]
    print(f"market {m['slug']} opens in {start - now:.0f} s; 3 post-only BUY {SIZE} Up @ {', '.join(PRICES)}; "
          f"hold {hold:.0f} s")

    orders = []
    for p in PRICES:
        signed = c.create_limit_order(token_id=tok, price=p, size=SIZE, side="BUY", post_only=True)
        t = time.time()
        try:
            r = c.post_order(signed)
            d = r.model_dump(mode="json")
            o = {"price": p, "id": d.get("order_id"), "t_post": t, "post_ms": round((time.time() - t) * 1000),
                 "post_status": d.get("status"), "ok": r.ok}
        except Exception as e:
            o = {"price": p, "id": None, "t_post": t, "ok": False, "error": _api_error(e)}
        print(f"  post {p}: ok={o['ok']} status={o.get('post_status')} {o.get('post_ms', '')} ms {o.get('error', '')}")
        orders.append(o)
    live = [o for o in orders if o["id"]]
    if not live:
        sys.exit("no order accepted")

    t0 = min(o["t_post"] for o in live)
    polls = []
    try:
        while time.time() - t0 < hold:
            for o in live:
                if o.get("ended"):
                    continue
                t = time.time()
                try:
                    st = c.get_order(order_id=o["id"]).status
                except Exception as e:
                    st = f"error: {_api_error(e)}"[:120]
                polls.append((round(t - o["t_post"], 2), o["price"], st))
                if "first_status" not in o:
                    o["first_status"] = st
                    print(f"  {o['price']}: first status {st}")
                if "LIVE" not in st.upper() and "OPEN" not in st.upper():
                    o.update(ended=st, ended_after_s=round(t - o["t_post"], 1))
                    print(f"  {o['price']}: status {st} after {o['ended_after_s']} s (not our cancel)")
            if all(o.get("ended") for o in live):
                break
            time.sleep(1.0)
    finally:
        mine = [o["id"] for o in live if not o.get("ended")]
        if mine:
            t = time.time()
            r = g.cancel_batch(mine)
            print(f"  our cancel after {t - t0:.0f} s: {len(r['canceled'])} cancelled, not: {r['not_canceled'] or '-'}"
                  f" ({r['ms']:.0f} ms)")
            for o in live:
                if o["id"] in r["canceled"]:
                    o.update(survived_s=round(t - o["t_post"], 1), cancelled_by_us=True)
        time.sleep(1.5)
        still = [x["id"] for x in g.open_orders_in(m["conditionId"]) if x["id"] in {o["id"] for o in live}]
        if still:
            print("  sweep: still open, cancelling", len(still), g.cancel_batch(still))
        print("  open after sweep:", len([x for x in g.open_orders_in(m["conditionId"])
                                         if x["id"] in {o["id"] for o in live}]))

    print("\nresult:")
    for o in live:
        if o.get("cancelled_by_us"):
            print(f"  {o['price']}: SURVIVED {o['survived_s']} s (cancelled by us)")
        else:
            print(f"  {o['price']}: ended by the exchange after {o.get('ended_after_s')} s, status {o.get('ended')}")
    out = Path("data/probe")
    out.mkdir(parents=True, exist_ok=True)
    f = out / time.strftime("resting-%Y%m%d-%H%M%S.json", time.gmtime())
    f.write_text(json.dumps({"slug": m["slug"], "hold_s": hold, "orders": orders, "polls": polls}, default=str))
    print("saved", f)


if __name__ == "__main__":
    main()

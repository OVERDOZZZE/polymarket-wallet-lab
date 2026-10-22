"""ONE test order to see if Polymarket accepts orders from this server's location.

Post-only BUY 5 shares @ $0.01 of the NEXT BTC 5m window's Up token (cost if it ever filled: $0.05), then
cancel it straight away. Prints only the outcome, error text and timings - never keys or wallet data.
Run on the server from ~/app:  .venv/bin/python scripts/geotest_order.py
"""
import sys
import time

sys.path.insert(0, ".")
from backend.polymarket.markets import fetch_market  # noqa: E402
from backend.trading.gateway import Gateway, _api_error  # noqa: E402

g = Gateway()
g._init()
if not g.enabled:
    sys.exit(f"trading client not available: {g.reason}")
slug = f"btc-updown-5m-{int(time.time()) // 300 * 300 + 300}"
tok = fetch_market(slug)["tokens"]["up"]
print("market:", slug, "| order: post-only BUY 5 Up @ 0.01 ($0.05)")
order = g.client.create_limit_order(token_id=tok, price="0.01", size="5", side="BUY", post_only=True)
t = time.perf_counter()
oid = None
try:
    r = g.client.post_order(order)
    d = r.model_dump(mode="json")
    print(f"post_order: ok={r.ok} status={d.get('status')} message={getattr(r, 'message', None)!r} "
          f"code={getattr(r, 'code', None)!r}  ({(time.perf_counter() - t) * 1000:.0f} ms)")
    oid = d.get("order_id")
except Exception as e:
    print(f"post_order REJECTED ({(time.perf_counter() - t) * 1000:.0f} ms): {_api_error(e)}")
if oid:
    t = time.perf_counter()
    try:
        c = g.client.cancel_order(order_id=oid).model_dump(mode="json")
        print(f"cancel: canceled={c.get('canceled')} not_canceled={c.get('not_canceled')}  "
              f"({(time.perf_counter() - t) * 1000:.0f} ms)")
    except Exception as e:
        print("cancel FAILED:", _api_error(e))
    still = any(o.model_dump(mode="json").get("id") == oid for o in g.client.list_open_orders().iter_items())
    print("still open after cancel:", still)
ts = []
for _ in range(10):
    t = time.perf_counter()
    g.client.get_closed_only_mode()
    ts.append((time.perf_counter() - t) * 1000)
print(f"authenticated CLOB round trip ms: min {min(ts):.0f} median {sorted(ts)[5]:.0f}")

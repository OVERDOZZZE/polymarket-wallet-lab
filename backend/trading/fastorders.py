"""Low-latency order path for bots, on a Gateway's SDK client (added 2026-10-26; Gateway itself is unchanged).

Why: imb-maker-live's orders took p50 121 / p90 937 ms from decision to the book, while the HTTP POST alone took
p50 69 ms. The rest was waiting: a 4-thread pool shared with other work, batches of up to 15 orders (p90 1.5-2 s),
and cancels that waited for the post's ack (the order id came from the response). This path removes the waiting:
  * the order id is the EIP-712 hash of the signed order (what the exchange contract calls the order hash), so
    it is known when the order is signed, and a cancel can be sent while the post is still in flight;
  * one order per request, each on its own worker thread, all multiplexed over the client's warm HTTP/2
    connection (no batch waits for its slowest order, no queue: `workers` threads, far more than in flight);
  * cancels have their own threads, so they never wait behind posts;
  * nothing else runs on these threads (no DB writes, no refreshes): results go back through a callback.
The first orders check the local id against the exchange's: on a mismatch `verified` turns False and callers
must fall back to cancelling only acked ids.

Measured 2026-10-27 (scripts/delay_probe.py, packet capture): Polymarket answers posts and cancels in ~30 ms, but
when a cancel reaches it while a post is not answered yet, it can hold that post's response 0.6-2 s (live: up to
9 s), on BTC 5m windows that are open. Hence `cancel_gw`: cancels go over a second client (own TCP connection), so a
held post or a dropped post connection never delays them, and a cancel whose connection drops is resent at once.
A post that fails in transit is reported with code "transport": it was sent and may rest on the book.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from eth_utils.crypto import keccak

ZERO32 = "0x" + "00" * 32


class FastOrders:
    def __init__(self, gw, workers=48, cancel_gw=None):
        self.gw, self.cgw = gw, cancel_gw   # cancel_gw: its own client for cancels (used once it is enabled)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fast-order")
        self.cpool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="fast-cancel")   # never behind posts
        self._exch = {}                     # token -> (exchange address, protocol version)
        self.verified = None                # None: not checked yet; True / False after the first acks
        self.mismatch = 0
        self.rl = {"tier": None, "remaining": None, "min_remaining": None, "warnings": 0, "limited": 0}
        self._hooked = set()

    # ------------------------------------------------------------------ rate-limit headers (Poly-RateLimit-*)
    def _client(self, gw):
        """The SDK client of `gw`, with our listener on its CLOB order transport (set once per client)."""
        c = gw.client
        if id(c) not in self._hooked:
            self._hooked.add(id(c))
            try:
                c._ctx.secure_clob._on_rate_limit_update = self._on_rl
            except AttributeError:
                pass
        return c

    def _on_rl(self, u):
        rl = self.rl
        rl["tier"] = u.tier or rl["tier"]
        if u.remaining is not None:
            rl["remaining"] = u.remaining
            rl["min_remaining"] = u.remaining if rl["min_remaining"] is None else min(rl["min_remaining"], u.remaining)
        rl["warnings"] += bool(u.warning)

    # ------------------------------------------------------------------ signing
    def _ctx(self):
        return self.gw.client._ctx

    def _exchange(self, token):
        e = self._exch.get(token)
        if e is None:
            from polymarket._internal.actions.orders.context import resolve_order_exchange_address
            from polymarket._internal.protocol import is_v2_position_id
            ctx = self._ctx()
            meta = ctx.order_metadata.resolve_market(ctx, token_id=token)
            e = self._exch[token] = (resolve_order_exchange_address(ctx.environment_config, asset_id=token,
                                                                    neg_risk=meta.neg_risk),
                                     "3" if is_v2_position_id(token) else "2")
        return e

    def order_id(self, signed, token):
        """The exchange's order hash of a signed order: keccak(0x1901 | domain separator | struct hash)."""
        from polymarket._internal.actions.orders.typed_data import _app_domain_separator, _order_contents_hash
        from polymarket._internal.actions.orders.types import UnsignedOrder
        exch, ver = self._exchange(token)
        u = UnsignedOrder(chain_id=self._ctx().environment_config.chain_id, builder=signed.builder,
                          exchange_address=exch, expiration=signed.expiration, maker=signed.maker,
                          maker_amount=signed.maker_amount, metadata=signed.metadata, order_type=signed.order_type,
                          salt=signed.salt, side=signed.side, signature_type=signed.signature_type, signer=signed.signer,
                          taker_amount=signed.taker_amount, timestamp=signed.timestamp, token_id=signed.token_id,
                          protocol_version=ver)
        ds = bytes.fromhex(_app_domain_separator(u, protocol_version=ver)[2:])
        return "0x" + keccak(b"\x19\x01" + ds + _order_contents_hash(u)).hex()

    def sign(self, token, price, size, post_only=True):
        """-> (signed order, local order id, sign ms)."""
        t0 = time.perf_counter()
        signed = self.gw.client.create_limit_order(token_id=token, price=str(Decimal(str(round(price, 4)))),
                                                   size=f"{size:.2f}", side="BUY", post_only=post_only)
        oid = self.order_id(signed, token)
        return signed, oid, (time.perf_counter() - t0) * 1000

    # ------------------------------------------------------------------ requests (run on self.pool)
    def _post(self, signed, oid):
        from polymarket.errors import RateLimitError, TransportError
        t0 = time.perf_counter()
        err, r, code = None, None, "exception"
        try:
            r = self._client(self.gw).post_order(signed)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
            if isinstance(e, TransportError):
                code = "transport"          # sent, no answer: the order may rest on the book
            elif isinstance(e, RateLimitError):
                code = "rate_limited"
                self.rl["limited"] += 1
        ms = (time.perf_counter() - t0) * 1000
        ok = bool(r is not None and r.ok)
        sid = str(r.order_id) if ok else None
        if ok and sid:
            same = sid.lower() == oid.lower()
            if not same:
                self.mismatch += 1
                self.verified = False
            elif self.verified is None:
                self.verified = True
        if not ok and err is None and r is not None:
            err = f"{r.message} ({getattr(r, 'code', None)})"
        return {"ok": ok, "order_id": sid or oid, "local_id": oid, "error": err,
                "code": None if ok else (getattr(r, "code", None) if r is not None else code),
                "status": str(r.status) if ok else "rejected", "post_ms": round(ms, 1), "t_done": time.time()}

    def _cancel(self, ids):
        from polymarket.errors import TransportError
        gw = self.cgw if self.cgw is not None and self.cgw.enabled else self.gw
        t0 = time.perf_counter()
        out = None
        for attempt in range(2):            # a dropped connection: resend at once (cancelling is always safe)
            try:
                r = self._client(gw).cancel_orders(order_ids=list(ids))
                out = {"canceled": [str(x) for x in r.canceled],
                       "not_canceled": {str(k): v for k, v in r.not_canceled.items()}, "error": None, "retried": attempt}
                break
            except TransportError as e:
                out = {"canceled": [], "not_canceled": {}, "error": f"{type(e).__name__}: {e}"[:300]}
            except Exception as e:
                out = {"canceled": [], "not_canceled": {}, "error": f"{type(e).__name__}: {e}"[:300]}
                break
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["t_done"] = time.time()
        return out

    # ------------------------------------------------------------------ public: submit, never block
    def post(self, signed, oid):
        """Submit one signed order; returns a concurrent.futures.Future of the result dict."""
        return self.pool.submit(self._post, signed, oid)

    def cancel(self, ids):
        """Submit one cancel request for these ids (one id per request is fastest; a few are fine)."""
        return self.cpool.submit(self._cancel, list(ids))

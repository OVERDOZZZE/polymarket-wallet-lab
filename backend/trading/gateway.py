"""Live trading on Polymarket with the wallet in .env.live - built for the lowest order latency.

Uses Polymarket's official SDK `polymarket-client` (CLOB V2 orders; the old py-clob-client is archived and its
orders are rejected with "invalid order version"). The SDK detects the wallet type itself (EOA, POLY_PROXY,
GNOSIS_SAFE or DEPOSIT_WALLET - this account is a DEPOSIT_WALLET).

Latency path of one click:  browser --(open websocket /ws/trade)--> Gateway.place()
  --> sign (local, ~0.8 ms) --> POST /order on an already-open HTTP/2 connection --> CLOB.
What makes it fast:
  * one SecureClient for the whole server life; L2 creds from .env (no derive round trip);
  * the connection is kept warm by a cheap authenticated call every few seconds;
  * each upcoming window's tokens are pre-warmed by signing a throwaway order (never posted), which makes
    the SDK cache tick size / fee data, so the first real order of a window costs ~1 ms to sign, not ~400 ms;
  * market orders carry their worst acceptable price from the browser's live book (no book fetch);
  * order work runs on a dedicated thread pool, never on the event loop.
Fills and order updates stream back from the CLOB user channel and are pushed to the UI and the DB.

Safety: orders are refused until the UI arms trading (resets on every server start), and any single order
above LIVE_MAX_ORDER_USD (.env.live, default 500) is refused. Trading approvals are only set up when the user
presses the button for it (setup_approvals).
"""
import asyncio
import collections
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .. import db
from ..config import CLOB_USER_WS, DATA_API, live_env
from ..polymarket.markets import VARIANT, fetch_market, window_slug
from ..util import pct

KEEPALIVE_S = 8
POLL_BALANCE_S = 10
POLL_POSITIONS_S = 5
HTTP = requests.Session()


class Gateway:
    def __init__(self):
        self.client = None
        self.enabled = False
        self.reason = "starting"
        self.armed = False
        self.wallet = ""
        self.wallet_type = None
        self.builder = False
        self.approvals = None                             # {"complete": bool, "missing": int}
        self.max_order_usd = 500.0
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="trade")   # orders / cancels only
        self.bg = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trade-bg")  # refreshes: never delay an order
        self.rtt = collections.deque(maxlen=200)          # keep-alive round trips (ms)
        self.order_lat = collections.deque(maxlen=200)    # click-to-ack inside the server (ms)
        self.balance = None
        self.balance_ts = 0.0
        self.token_balances = {}                          # token_id -> shares
        self._bal_lock = threading.Lock()
        self._bal_again = False
        self.positions, self.positions_ts = [], 0.0
        self.open_orders = {}                             # order id -> order dict
        self.markets = {}                                 # slug -> market info
        self.warm = set()                                 # token ids whose order data the SDK has cached
        self.user_ws = "stopped"
        self.subscribers = set()                          # asyncio.Queue per connected browser
        self.loop = None
        self.errors = collections.deque(maxlen=20)
        self.reconnects = 0                               # CLOB HTTP/2 connections recycled (see _keepalive)
        self.light = False                                # bot process: poll USDC only (no token balances/positions)
        self.on_event = None                              # bot process: called on the loop with each user-channel event

    # ------------------------------------------------------------------ lifecycle
    def start(self, loop):
        self.loop = loop
        threading.Thread(target=self._init, daemon=True, name="trade-init").start()
        loop.create_task(self._user_channel())
        loop.create_task(self._market_scheduler())

    def _init(self):
        env = live_env()
        pk, self.wallet = env.get("POLYMARKET_PRIVATE_KEY"), (env.get("POLYMARKET_WALLET_ADDRESS") or "").lower()
        self.max_order_usd = float(env.get("LIVE_MAX_ORDER_USD") or 500)
        if not pk:
            self.reason = "No wallet: add POLYMARKET_PRIVATE_KEY (and POLYMARKET_WALLET_ADDRESS) to .env.live"
            return
        try:
            from polymarket import ApiKeyCreds, BuilderApiKey, SecureClient
            creds = None
            if env.get("POLY_API_KEY") and env.get("POLY_API_SECRET") and env.get("POLY_API_PASSPHRASE"):
                creds = ApiKeyCreds(key=env["POLY_API_KEY"], secret=env["POLY_API_SECRET"],
                                    passphrase=env["POLY_API_PASSPHRASE"])
            builder = None
            if env.get("POLYMARKET_BUILDER_API_KEY") and env.get("POLYMARKET_BUILDER_SECRET"):
                builder = BuilderApiKey(key=env["POLYMARKET_BUILDER_API_KEY"], secret=env["POLYMARKET_BUILDER_SECRET"],
                                        passphrase=env.get("POLYMARKET_BUILDER_PASSPHRASE") or "")
            self.client = SecureClient.create(private_key=pk, wallet=self.wallet or None, credentials=creds,
                                              api_key=builder)
            self.wallet, self.wallet_type, self.builder = str(self.client.wallet).lower(), self.client.wallet_type, bool(builder)
            self.enabled, self.reason = True, ""
        except Exception as e:
            self.reason = f"Could not start trading client: {type(e).__name__}: {e}"[:300]
            return
        threading.Thread(target=self._keepalive, daemon=True, name="trade-keepalive").start()
        threading.Thread(target=self._poller, daemon=True, name="trade-poller").start()
        self.refresh_orders()
        self.refresh_approvals()

    def _keepalive(self):
        """The CLOB ends an HTTP/2 connection after 10,000 requests (GOAWAY, last_stream_id 19999) or when idle
        ("Server disconnected"); the call that hits it fails. Reconnect at once so the next order finds a warm
        connection, instead of paying a TLS handshake. Only a failed reconnect is an error."""
        while True:
            t0 = time.perf_counter()
            try:
                self.client.get_closed_only_mode()            # cheap authenticated CLOB call
                self.rtt.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                if not _conn_dropped(e):
                    self._err(f"keepalive: {e}")
                else:
                    self.reconnects += 1
                    try:
                        self.client.get_closed_only_mode()
                    except Exception as e2:
                        self._err(f"keepalive reconnect: {e2}")
            time.sleep(KEEPALIVE_S)

    def _poller(self):
        while True:
            now = time.time()
            if now - self.balance_ts >= POLL_BALANCE_S:
                self.refresh_balances()
            if not self.light and now - self.positions_ts >= POLL_POSITIONS_S:
                self.refresh_positions()
            time.sleep(1.0)

    def _err(self, msg):
        self.errors.append((time.time(), str(msg)[:300]))

    # ------------------------------------------------------------------ push to browsers
    def publish(self, msg):
        if not self.loop:
            return
        data = json.dumps(msg, default=str)

        def put():
            for q in list(self.subscribers):
                if q.qsize() < 1000:
                    q.put_nowait(data)
        self.loop.call_soon_threadsafe(put)

    # ------------------------------------------------------------------ markets (current + next window)
    def window_list(self, asset="BTC"):
        now = time.time()
        out = []
        for mins in VARIANT:
            L = mins * 60
            s = int(now) // L * L
            for start in (s, s + L):
                m = self.markets.get(window_slug(asset, mins, start))
                if m:
                    out.append(m)
        return out

    async def _market_scheduler(self):
        """Resolve current + next 5m/15m BTC windows and pre-warm their order data."""
        while True:
            try:
                now = time.time()
                for mins in VARIANT:
                    L = mins * 60
                    s = int(now) // L * L
                    for start in (s, s + L, s + 2 * L):
                        slug = window_slug("BTC", mins, start)
                        if slug not in self.markets:
                            try:
                                info = await asyncio.to_thread(fetch_market, slug)
                            except Exception as e:
                                info = None
                                self._err(f"gamma {slug}: {e}")
                            if info and "up" in info["tokens"]:
                                info["end_ts"] = start + L
                                self.markets[slug] = info
                        m = self.markets.get(slug)
                        if m and self.client and start - now < 120:
                            for tok in m["tokens"].values():
                                if tok not in self.warm:
                                    await asyncio.get_running_loop().run_in_executor(self.pool, self._warm_token, tok)
                for slug, m in list(self.markets.items()):
                    if now > m["end_ts"] + 600:
                        self.markets.pop(slug, None)
            except Exception as e:
                self._err(f"scheduler: {e}")
            await asyncio.sleep(2.0)

    def _warm_token(self, tok):
        """Sign (never post) a tiny order so the SDK caches this token's tick size and fee data."""
        try:
            self.client.create_limit_order(token_id=tok, price="0.01", size="5", side="BUY", post_only=True)
            self.warm.add(tok)
        except Exception as e:
            self._err(f"warm {tok[:8]}: {e}")

    # ------------------------------------------------------------------ orders
    def place(self, req):
        """req: slug, outcome up|down, side BUY|SELL, type market|limit, amount ($, market BUY),
        shares (SELL or limit), price (limit price, or worst acceptable price for market), post_only."""
        t0 = time.perf_counter()
        if not self.enabled:
            raise RuntimeError(self.reason or "trading is not available")
        if not self.armed:
            raise RuntimeError("Trading is not armed. Switch on Live trading in the order ticket first.")
        m = self.markets.get(req["slug"])
        if not m:
            raise RuntimeError("Unknown market (window not loaded yet)")
        outcome = req["outcome"].lower()
        token = m["tokens"][outcome]
        side = req["side"].upper()
        tick = m["tick"]
        price = round(min(max(float(req["price"]), tick), 1 - tick), 4)
        if req["type"] == "market":
            amount, size = float(req["amount"] if side == "BUY" else req["shares"]), None
            self._check_size(amount if side == "BUY" else amount * price)
            if side == "BUY":
                order = self.client.create_market_order(token_id=token, side="BUY", amount=f"{amount:.2f}",
                                                        max_price=str(price), order_type="FAK")
            else:   # round DOWN: rounding 9.976 held shares up to 9.98 is refused as "not enough balance"
                order = self.client.create_market_order(token_id=token, side="SELL", shares=f"{_floor2(amount):.2f}",
                                                        min_price=str(price), order_type="FAK")
        else:
            size, amount = float(req["shares"]), None
            if side == "SELL":
                size = _floor2(size)
            self._check_size(size * price)
            order = self.client.create_limit_order(token_id=token, price=str(price), size=f"{size:.2f}", side=side,
                                                   post_only=bool(req.get("post_only")))
        t_signed = time.perf_counter()
        resp, err = None, None
        try:
            resp = self.client.post_order(order)
            if not resp.ok:
                err = f"{resp.message} ({resp.code})" if getattr(resp, "code", None) else resp.message
        except Exception as e:
            err = _api_error(e)
        t_done = time.perf_counter()
        sign_ms, post_ms, total_ms = (t_signed - t0) * 1000, (t_done - t_signed) * 1000, (t_done - t0) * 1000
        self.order_lat.append(total_ms)
        r = resp.model_dump(mode="json") if resp is not None else None
        oid = (r or {}).get("order_id") or f"rejected-{time.time_ns()}"
        status = (r or {}).get("status") or "rejected"
        db.x("INSERT OR REPLACE INTO live_orders (order_id, ts, origin, slug, token_id, outcome, side, order_type, "
             "price, size, amount, status, latency_ms, error, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (oid, time.time(), req.get("origin", "manual"), m["slug"], token, outcome, side, req["type"], price, size,
              amount, str(status), total_ms, err,
              db.dumps({"req": req, "resp": r, "sign_ms": round(sign_ms, 2), "post_ms": round(post_ms, 1)})))
        out = {"ok": err is None, "order_id": oid, "status": status, "error": err, "sign_ms": round(sign_ms, 2),
               "post_ms": round(post_ms, 1), "total_ms": round(total_ms, 1), "resp": r,
               "making": (r or {}).get("making_amount"), "taking": (r or {}).get("taking_amount")}
        self.bg.submit(self._after_order)
        return out

    def _check_size(self, usd):
        if usd > self.max_order_usd:
            raise RuntimeError(f"Order ${usd:,.2f} is above the ${self.max_order_usd:,.0f} limit "
                               "(LIVE_MAX_ORDER_USD in .env.live)")
        if usd <= 0:
            raise RuntimeError("Order size must be positive")

    def _after_order(self):
        time.sleep(0.3)
        self.refresh_orders()
        self.refresh_balances()
        self._refresh_soon()

    def _refresh_soon(self, delays=(2.0, 5.0, 10.0)):
        """Share balances change only once a fill settles on-chain (seconds after the match), so look again a
        few times; otherwise the ticket shows 0 shares held and Sell stays disabled until the next poll."""
        for d in delays:
            t = threading.Timer(d, lambda: self.bg.submit(self.refresh_balances))
            t.daemon = True
            t.start()

    def post_limit_batch(self, orders, origin):
        """A market maker's path: post-only GTC BUY limit orders [(slug, outcome, token, price, shares)], at most 15
        (one POST /orders), with no balance / order refreshes afterwards (a maker sends thousands per hour; fills
        come on the user channel). Every order is stored in live_orders. Returns per order {ok, order_id, error,
        code} in input order, plus sign / post ms."""
        if not self.enabled:
            raise RuntimeError(self.reason or "trading is not available")
        if not self.armed:
            raise RuntimeError("Trading is not armed.")
        t0 = time.perf_counter()
        signed = []
        for _slug, _outcome, token, price, size in orders:
            self._check_size(size * price)
            signed.append(self.client.create_limit_order(token_id=token, price=str(round(price, 4)), size=f"{size:.2f}",
                                                         side="BUY", post_only=True))
        t_signed = time.perf_counter()
        resps, err = None, None
        try:
            resps = self.client.post_orders(signed)
        except Exception as e:
            err = _api_error(e)
        t_done = time.perf_counter()
        sign_ms, post_ms, total_ms = (t_signed - t0) * 1000, (t_done - t_signed) * 1000, (t_done - t0) * 1000
        self.order_lat.append(total_ms)
        now, out, rows = time.time(), [], []
        for i, (slug, outcome, token, price, size) in enumerate(orders):
            r = resps[i] if resps is not None and i < len(resps) else None
            ok = bool(r is not None and r.ok)
            oid = str(r.order_id) if ok else f"rejected-{time.time_ns()}-{i}"
            e = None if ok else (err or (f"{r.message} ({r.code})" if r is not None else "no response"))
            code = None if ok else (getattr(r, "code", None) or "error")
            status = str(r.status) if ok else "rejected"
            out.append({"ok": ok, "order_id": oid, "error": e, "code": code, "status": status})
            rows.append((oid, now, origin, slug, token, outcome, "BUY", "limit", price, size, None, status, total_ms, e,
                         db.dumps({"post_only": True, "batch": len(orders), "sign_ms": round(sign_ms, 2),
                                   "post_ms": round(post_ms, 1)})))
        db.many("INSERT OR REPLACE INTO live_orders (order_id, ts, origin, slug, token_id, outcome, side, order_type, "
                "price, size, amount, status, latency_ms, error, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return {"orders": out, "sign_ms": round(sign_ms, 2), "post_ms": round(post_ms, 1), "total_ms": round(total_ms, 1)}

    def cancel_batch(self, order_ids):
        """Cancel these orders in one request (allowed while disarmed: cancelling is always safe). No refreshes."""
        if not self.enabled:
            raise RuntimeError(self.reason or "trading is not available")
        t0 = time.perf_counter()
        r = self.client.cancel_orders(order_ids=list(order_ids))
        return {"canceled": [str(x) for x in r.canceled], "not_canceled": {str(k): v for k, v in r.not_canceled.items()},
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    def merge_max(self, condition_id):
        """Merge every balanced Up + Down pair of a market back into USDC (gasless, via Polymarket's relayer); blocks
        until the transaction is final."""
        if not self.enabled:
            raise RuntimeError(self.reason or "trading is not available")
        h = self.client.merge_positions(condition_id=condition_id, amount="max")
        res = h.wait()
        return str(getattr(res, "transaction_hash", None) or getattr(res, "status", None) or res)[:120]

    def cancel(self, order_id):
        r = self.client.cancel_order(order_id=order_id)
        self.bg.submit(self.refresh_orders)
        return r.model_dump(mode="json")

    def cancel_all(self):
        r = self.client.cancel_all()
        self.bg.submit(self.refresh_orders)
        return r.model_dump(mode="json")

    def refresh_orders(self):
        if not self.client:
            return
        try:
            orders = [o.model_dump(mode="json") for o in self.client.list_open_orders().iter_items()]
            self.open_orders = {o["id"]: o for o in orders}
            self.publish({"type": "orders", "orders": orders})
        except Exception as e:
            self._err(f"orders: {_api_error(e)}")

    def refresh_balances(self):
        if not self.client:
            return
        if not self._bal_lock.acquire(blocking=False):   # one already running: it runs once more when done
            self._bal_again = True
            return
        try:
            while True:
                self._bal_again = False
                self._refresh_balances()
                if not self._bal_again:
                    break
        finally:
            self._bal_lock.release()

    def _refresh_balances(self):
        self.balance_ts = time.time()
        try:
            self.balance = int(self._retry(lambda: self.client.get_balance_allowance(asset_type="COLLATERAL")).balance) / 1e6
        except Exception as e:
            self._err(f"balance: {_api_error(e)}")
        for m in [] if self.light else self.window_list():
            if m["start"] > time.time() + 60:
                continue
            for tok in m["tokens"].values():
                try:   # shares can sit in either conditional-token generation; count both
                    self.token_balances[tok] = sum(
                        int(self._retry(lambda t=t: self.client.get_balance_allowance(asset_type=t, token_id=tok)).balance)
                        for t in ("CONDITIONAL", "CONDITIONAL-V2")) / 1e6
                except Exception as e:   # one failed token must not freeze all balances
                    self._err(f"balance {tok[:8]}: {_api_error(e)}")
        self.publish({"type": "balances", "usdc": self.balance, "tokens": self.token_balances})

    @staticmethod
    def _retry(fn, tries=2):
        """One quick retry: the kept-alive connection is sometimes dropped by the server ("Server disconnected")."""
        for i in range(tries):
            try:
                return fn()
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(0.2)

    def refresh_positions(self):
        self.positions_ts = time.time()
        if not self.wallet:
            return
        try:
            pos = HTTP.get(f"{DATA_API}/positions", timeout=10, params={
                "user": self.wallet, "limit": 100, "sizeThreshold": 0.01, "sortBy": "CURRENT",
                "sortDirection": "DESC"}).json()
            self.positions = pos if isinstance(pos, list) else []
            self.publish({"type": "positions", "positions": self.positions})
        except Exception as e:
            self._err(f"positions: {e}")

    # ------------------------------------------------------------------ trading approvals (user-initiated only)
    def refresh_approvals(self):
        try:
            st = self.client.get_trading_approvals_state()
            missing = len(st.missing.erc20) + len(st.missing.erc1155)
            self.approvals = {"complete": bool(st.is_fully_approved), "missing": missing}
        except Exception as e:
            self._err(f"approvals: {_api_error(e)}")

    def setup_approvals(self):
        """Send the missing approval transactions (gasless via Polymarket's relayer). Only on user request."""
        if not self.enabled:
            raise RuntimeError(self.reason or "trading is not available")
        handle = self.client.setup_trading_approvals()
        try:
            handle.wait()
        except Exception as e:
            self._err(f"approvals tx: {_api_error(e)}")
        self.refresh_approvals()
        return self.approvals

    # ------------------------------------------------------------------ CLOB user channel (our fills/orders)
    async def _user_channel(self):
        import websockets
        while not self.enabled:
            await asyncio.sleep(1.0)
        c = self.client.credentials
        sub = {"auth": {"apiKey": c.key, "secret": c.secret, "passphrase": c.passphrase}, "markets": [], "type": "user"}
        backoff, drops = 1, collections.deque(maxlen=10)
        while True:
            up_since = None
            try:
                self.user_ws = "connecting"
                async with websockets.connect(CLOB_USER_WS, ping_interval=None, open_timeout=10, close_timeout=1,
                                              max_size=None) as ws:
                    await ws.send(json.dumps(sub))
                    self.user_ws, last_ping = "connected", time.time()
                    up_since = last_rx = time.time()
                    if drops:                              # catch up on fills/order changes sent while we were away
                        self.bg.submit(self._after_order)
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            last_rx = time.time()
                            if raw not in ("PONG", b"PONG"):
                                self._on_user(raw)
                        except asyncio.TimeoutError:
                            pass
                        if time.time() - last_rx > 30:     # PINGs every 9 s go unanswered: half-open socket
                            raise ConnectionError("no PONG for 30 s")
                        if time.time() - last_ping > 9:
                            await ws.send("PING")
                            last_ping = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Polymarket drops this socket now and then without a close frame; reconnecting at once and
                # resyncing is the normal path, so only report it when it keeps happening.
                self.user_ws = "reconnecting"
                now = time.time()
                drops.append(now)
                if up_since and now - up_since > 60:
                    backoff = 1
                recent = sum(1 for t in drops if now - t < 600)
                if recent >= 3 or not up_since:
                    self._err(f"user channel: {type(e).__name__}: {e} ({recent} drops in 10 min)")
                await asyncio.sleep(backoff if not up_since or recent > 1 else 0.2)
                backoff = min(backoff * 2, 30)

    def _on_user(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        for e in d if isinstance(d, list) else [d]:
            et = e.get("event_type")
            if et == "trade":
                slug = next((m["slug"] for m in self.markets.values() if m.get("conditionId") == e.get("market")), None)
                try:
                    db.x("INSERT OR REPLACE INTO live_trades (id, ts, order_id, slug, token_id, outcome, side, price, "
                         "size, status, data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (e.get("id"), time.time(), e.get("taker_order_id"), slug, e.get("asset_id"),
                          str(e.get("outcome") or "").lower(), e.get("side"), float(e.get("price") or 0),
                          float(e.get("size") or 0), e.get("status"), db.dumps(e)))
                except Exception as ex:
                    self._err(f"store trade: {ex}")
                self.publish({"type": "trade", "trade": e, "slug": slug})
                # balances move when the fill settles on-chain (MINED / CONFIRMED), not at MATCHED
                if e.get("status") in ("MATCHED", "MINED", "CONFIRMED", None):
                    self.bg.submit(self.refresh_balances)
                    if not self.light:
                        self.bg.submit(self.refresh_positions)
                if e.get("status") in ("MATCHED", None):
                    self._refresh_soon()
            elif et == "order":
                oid = e.get("id")
                if e.get("type") == "CANCELLATION" or (float(e.get("size_matched") or 0) >= float(e.get("original_size") or 1e18)):
                    self.open_orders.pop(oid, None)
                else:
                    self.open_orders[oid] = {**self.open_orders.get(oid, {}), **e}
                db.x("UPDATE live_orders SET status = ?, filled = ? WHERE order_id = ?",
                     (e.get("type"), float(e.get("size_matched") or 0), oid))
                self.publish({"type": "order", "order": e, "orders": list(self.open_orders.values())})
            if self.on_event and et in ("trade", "order"):
                try:
                    self.on_event(e)
                except Exception as ex:
                    self._err(f"on_event: {type(ex).__name__}: {ex}")

    # ------------------------------------------------------------------ status for the UI
    def status(self):
        return {"enabled": self.enabled, "reason": self.reason, "armed": self.armed,
                "wallet": self.wallet, "wallet_type": self.wallet_type, "approvals": self.approvals,
                "builder": self.builder, "max_order_usd": self.max_order_usd, "usdc": self.balance,
                "token_balances": self.token_balances, "user_ws": self.user_ws,
                "rtt_ms": {"p50": pct(list(self.rtt), .5), "p90": pct(list(self.rtt), .9), "last": self.rtt[-1] if self.rtt else None},
                "order_ms": {"p50": pct(list(self.order_lat), .5), "last": self.order_lat[-1] if self.order_lat else None},
                "warm_tokens": len(self.warm), "reconnects": self.reconnects, "errors": list(self.errors)[-8:]}


def _floor2(x):
    return int(float(x) * 100 + 1e-6) / 100.0


def _api_error(e):
    for attr in ("message", "error_msg"):
        v = getattr(e, attr, None)
        if v:
            return v if isinstance(v, str) else json.dumps(v, default=str)
    return f"{type(e).__name__}: {e}"[:300]


def _conn_dropped(e):
    """The SDK's TransportError over httpx's RemoteProtocolError: the server closed the connection under us."""
    import httpx
    return isinstance(e.__cause__, httpx.RemoteProtocolError)


gateway = Gateway()

"""Market-data recorder for bot research:  python -m backend.recorder

Writes every raw message the bots react to, stamped with our receive time, so a strategy can be replayed
offline through the same PaperBot engine (backend/bots/replay.py) instead of waiting hours of paper trading:
  * CLOB market websocket of every BTC 5m and 15m window (current + next, opened PREOPEN_S early);
  * Coinbase BTC-USD ticker (the bots' spot feed), Binance BTCUSDT bookTicker (spot `bn`), USD-M futures aggTrade (`bf`, since 2026-10-22 14:40Z) and
    Kraken BTC/USD best bid/ask
    (research: which feed leads Polymarket's book from where the bots run);
  * RTDS Chainlink btc/usd ticks (settlement TWAP) and the RTDS activity stream filtered to BTC Up/Down
    windows (every wallet's fills: lets copy bots be replayed);
  * one `info` line per window (token ids, tick, fees) from Gamma.

Files: data/recordings/<YYYY-MM-DD>/<HH>.tsv.gz (UTC hour), one line per message:
    <recv_ms>\t<source>\t<raw message>
source = cb | bn | bf | kr | cl | act | info | bk:<slug>. Gzip members are flushed every few seconds, so a file being
written is readable. Old days are deleted after KEEP_DAYS, or sooner while the disk has < MIN_FREE_GB free.
"""
import asyncio
import gzip
import json
import queue
import shutil
import signal
import sys
import threading
import time

import requests

from .config import BINANCE_FUT_WS, BINANCE_WS, CLOB_MARKET_WS, COINBASE_WS, GAMMA, KRAKEN_WS, RECORDINGS_DIR, RTDS_WS


PREOPEN_S = 30
KEEP_DAYS = 7
MIN_FREE_GB = 10        # also delete the oldest days while the disk has less free space than this
WINDOWS = (5, 15)
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Mozilla/5.0 (pmtool recorder)"


class Writer(threading.Thread):
    """Gzip writer on its own thread: the socket loop only does queue.put."""

    def __init__(self):
        super().__init__(daemon=True, name="rec-writer")
        self.q = queue.Queue()
        self.n = 0

    def put(self, src, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        self.q.put((time.time() * 1000.0, src, raw))

    def run(self):
        f, key, last_flush = None, None, time.time()
        while True:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                item = (None, None, None)
            if item is None:                       # stop(): end the gzip member cleanly
                if f:
                    f.close()
                return
            ms, src, raw = item
            now = time.time()
            if ms is not None:
                k = time.strftime("%Y-%m-%d/%H", time.gmtime(ms / 1000.0))
                if k != key:
                    if f:
                        f.close()
                    path = RECORDINGS_DIR / f"{k}.tsv.gz"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    f, key = gzip.open(path, "at", encoding="utf-8", compresslevel=4), k
                    self._prune()
                f.write(f"{ms:.1f}\t{src}\t{raw.replace(chr(10), ' ').replace(chr(13), ' ')}\n")
                self.n += 1
            if f and now - last_flush > 5:
                f.flush()
                last_flush = now

    def stop(self, timeout=5.0):
        """Finish the open gzip member (a killed writer leaves it unterminated, and readers stop there)."""
        self.q.put(None)
        self.join(timeout)

    @staticmethod
    def _prune():
        days = sorted(p for p in RECORDINGS_DIR.iterdir() if p.is_dir())
        for p in days[:-KEEP_DAYS]:
            shutil.rmtree(p, ignore_errors=True)
        days = days[-KEEP_DAYS:]
        while len(days) > 1 and shutil.disk_usage(RECORDINGS_DIR).free < MIN_FREE_GB * 1e9:   # never today's
            print(f"{time.strftime('%H:%M:%S')} disk below {MIN_FREE_GB} GB free: deleting {days[0].name}", flush=True)
            shutil.rmtree(days.pop(0), ignore_errors=True)


W = Writer()


async def stream(url, src, subscribe=None, ping=None, keep=None, until=None, refused=None, silent_s=None):
    """Reconnecting websocket -> writer. `keep(raw)` filters, `until()` ends it. `refused` = (text, subscribe):
    a message containing `text` means the subscription was refused; send the fallback `subscribe` instead.
    `silent_s`: reconnect when nothing was kept for that long (RTDS can stay open and stop sending one symbol)."""
    import websockets
    backoff = 0.2
    while until is None or not until():
        try:
            async with websockets.connect(url, ping_interval=None if ping else 15, max_size=None, max_queue=None,
                                          open_timeout=10, compression="deflate") as ws:
                if subscribe:
                    await ws.send(json.dumps(subscribe))
                backoff, last_ping = 0.2, time.time()
                last_kept = time.time()
                while until is None or not until():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        if refused and refused[0] in (raw if isinstance(raw, str) else raw.decode("utf-8", "replace")):
                            print(f"{time.strftime('%H:%M:%S')} {src}: refused ({refused[0]}); fallback "
                                  f"{json.dumps(refused[1])}", flush=True)
                            await ws.send(json.dumps(refused[1]))
                            continue
                        if keep is None or keep(raw):
                            W.put(src, raw)
                            last_kept = time.time()
                    except asyncio.TimeoutError:
                        pass
                    if silent_s and time.time() - last_kept > silent_s:
                        raise TimeoutError(f"nothing kept for {silent_s:g} s, reconnecting")
                    if ping and time.time() - last_ping > 9:
                        await ws.send(ping)
                        last_ping = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} {src}: {type(e).__name__}: {e}"[:200], flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)


def fetch_info(slug):
    js = HTTP.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10).json()
    if not js:
        return None
    m = js[0]
    cmc, fs = m.get("cryptoMarketConfig") or {}, m.get("feeSchedule") or {}
    return {"slug": slug, "tokens": dict(zip([str(o).lower() for o in json.loads(m.get("outcomes") or "[]")],
                                             json.loads(m.get("clobTokenIds") or "[]"))),
            "conditionId": m.get("conditionId"), "tick": m.get("orderPriceMinTickSize") or 0.01,
            "min_size": m.get("orderMinSize") or 5, "twap": cmc.get("twapEnabled", True),
            "twap_s": cmc.get("twapLookbackSeconds") or 60, "fee_rate": fs.get("rate"),
            "rebate_rate": fs.get("rebateRate")}


async def books():
    tasks = {}
    while True:
        now = time.time()
        for mins in WINDOWS:
            L = mins * 60
            s = int(now) // L * L
            for start in (s, s + L) if s + L - now <= PREOPEN_S else (s,):
                slug = f"btc-updown-{mins}m-{start}"
                if slug in tasks:
                    continue
                try:
                    info = await asyncio.to_thread(fetch_info, slug)
                except Exception as e:
                    print(f"gamma {slug}: {e}"[:200], flush=True)
                    info = None
                if not info or "up" not in info["tokens"]:
                    continue
                W.put("info", json.dumps(info))
                end = start + L
                tasks[slug] = asyncio.create_task(stream(
                    CLOB_MARKET_WS, f"bk:{slug}", {"assets_ids": list(info["tokens"].values()), "type": "market"},
                    ping="PING", keep=lambda r: r not in ("PONG", b"PONG"), until=lambda e=end: time.time() > e + 5))
        for slug, t in list(tasks.items()):
            if t.done() and int(slug.rsplit("-", 1)[1]) + 3600 < now:
                tasks.pop(slug)
        await asyncio.sleep(0.5)


async def main():
    W.start()
    cl_subs = [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
               {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": "btc/usd"})}]
    jobs = [
        books(),
        # Coinbase allows ~10 ticker subscriptions per product per IP (the bots take the rest): then the trades
        stream(COINBASE_WS, "cb", {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]},
               refused=("subscription limit reached for channel: ticker",
                        {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches"]})),
        stream(BINANCE_WS, "bn"),
        stream(BINANCE_FUT_WS, "bf"),
        stream(KRAKEN_WS, "kr", {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"],
                                                                   "event_trigger": "bbo"}},
               keep=lambda r: '"ticker"' in (r if isinstance(r, str) else r.decode("utf-8", "replace"))),
        stream(RTDS_WS, "cl", {"action": "subscribe", "subscriptions": cl_subs},
               keep=lambda r: "btc/usd" in (r if isinstance(r, str) else r.decode("utf-8", "replace")), silent_s=20),
        stream(RTDS_WS, "act", {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]},
               keep=lambda r: "btc-updown" in (r if isinstance(r, str) else r.decode("utf-8", "replace"))),
    ]
    tasks = [asyncio.create_task(j) for j in jobs]

    async def heartbeat():
        while True:
            await asyncio.sleep(60)
            print(f"{time.strftime('%H:%M:%S')} recorded {W.n:,} messages, queue {W.q.qsize()}", flush=True)
    tasks.append(asyncio.create_task(heartbeat()))
    await asyncio.gather(*tasks)


def _term(*_):
    raise KeyboardInterrupt                        # systemd stop / restart: shut down like Ctrl+C


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _term)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        W.stop()
        sys.exit(0)

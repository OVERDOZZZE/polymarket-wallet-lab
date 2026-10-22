// Market data streamed by the BROWSER straight from each venue - no server hop, lowest display latency.
//   chainlink : Polymarket RTDS, btc/usd Chainlink stream (what Up/Down windows settle on)
//   coinbase  : Coinbase Exchange ticker BTC-USD (mid of best bid/ask)
//   kraken    : Kraken v2 trades BTC/USD
//   binance   : Binance trades BTCUSDT (USDT, so it carries a basis vs USD)
//   books     : Polymarket CLOB market channel, one socket per window (opened before the window starts)
// Consumers subscribe() and redraw on their own clock (requestAnimationFrame / throttled React state).

export type FeedId = "chainlink" | "coinbase" | "kraken" | "binance";
export const FEEDS: { id: FeedId; label: string; color: string }[] = [
  { id: "chainlink", label: "Chainlink", color: "var(--s1)" },
  { id: "coinbase", label: "Coinbase", color: "var(--s2)" },
  { id: "kraken", label: "Kraken", color: "var(--s3)" },
  { id: "binance", label: "Binance", color: "var(--s4)" },
];
export const FEED_HEX: Record<FeedId, string> = { chainlink: "#3987e5", coinbase: "#d95926", kraken: "#199e70", binance: "#c98500" };

const KEEP_MS = 30 * 60 * 1000;
const BUCKET_MS = 100; // at most one chart point per 100 ms per feed

export class Feed {
  id: FeedId;
  status = "connecting";
  t: number[] = [];
  v: number[] = [];
  last: number | null = null;
  lastRecv = 0;
  lat: number[] = []; // receive - exchange timestamp (ms), local clock
  basisObs: number[] = [];
  basis = 0; // chainlink - this feed (median), to overlay on the Chainlink scale
  msgs = 0;
  constructor(id: FeedId) {
    this.id = id;
  }
  push(recv: number, price: number, exTs?: number) {
    this.last = price;
    this.lastRecv = recv;
    this.msgs++;
    if (exTs) {
      this.lat.push(recv - exTs);
      if (this.lat.length > 400) this.lat.splice(0, 200);
    }
    const n = this.t.length;
    if (n && recv - this.t[n - 1] < BUCKET_MS) {
      this.v[n - 1] = price;
    } else {
      this.t.push(recv);
      this.v.push(price);
      if (this.t[0] < recv - KEEP_MS - 60000) {
        const cut = this.t.findIndex((x) => x >= recv - KEEP_MS);
        this.t.splice(0, cut);
        this.v.splice(0, cut);
      }
    }
    bump();
  }
  latency(): number | null {
    if (!this.lat.length) return null;
    const s = [...this.lat].sort((a, b) => a - b);
    return s[Math.floor(s.length / 2)];
  }
}

export const feeds: Record<FeedId, Feed> = {
  chainlink: new Feed("chainlink"),
  coinbase: new Feed("coinbase"),
  kraken: new Feed("kraken"),
  binance: new Feed("binance"),
};
/** Chainlink values stamped exactly on a second: sec -> price (RTDS sends one per second). */
export const clExact = new Map<number, number>();

/** The Up/Down markets settle on Chainlink's 60 s TWAP (btc-usd-twap-60s stream), at the window start (price
 *  to beat) and end (final price) - not on a single tick. That stream is not on RTDS, so it is computed from
 *  the 1 s ticks: mean of the ticks in (sec - 60, sec]. Matches Polymarket's numbers to ~2 cents.
 *  Same rule as backend/polymarket/markets.py:twap_of. */
export const TWAP_S = 60;
export function twapAt(sec: number, minTicks = 50): number | null {
  let sum = 0, n = 0;
  for (let s = sec - TWAP_S + 1; s <= sec; s++) {
    const v = clExact.get(s);
    if (v !== undefined) { sum += v; n++; }
  }
  return n >= minTicks ? sum / n : null;
}
/** Seconds of the newest Chainlink tick (exchange time), or null. */
export const clLastSec = () => (clTicks.length ? Math.floor(clTicks[clTicks.length - 1][0] / 1000) : null);
/** Where a window's final TWAP is heading at time `now`: the ticks of its last minute seen so far, plus `spot`
 *  for the seconds still to come. */
export function projectedTwap(end: number, spot: number | null): number | null {
  const last = clLastSec();
  if (last === null || spot === null) return null;
  let sum = 0, n = 0;
  for (let s = end - TWAP_S + 1; s <= Math.min(end, last); s++) {
    const v = clExact.get(s);
    if (v !== undefined) { sum += v; n++; }
  }
  const rest = Math.max(0, end - Math.max(last, end - TWAP_S));
  return n + rest ? (sum + spot * rest) / (n + rest) : null;
}
/** Chainlink exchange-time series (ms, price) for strike/settlement lookups. */
export const clTicks: [number, number][] = [];

// ------------------------------------------------------------------ change notification
let version = 0;
const subs = new Set<() => void>();
function bump() {
  version++;
}
export const getVersion = () => version;
export function subscribe(fn: () => void) {
  subs.add(fn);
  return () => { subs.delete(fn); };
}
// one notifier for everybody, at display rate
function notifyLoop() {
  let seen = -1;
  const tick = () => {
    if (version !== seen) {
      seen = version;
      subs.forEach((f) => f());
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ------------------------------------------------------------------ sockets
function connect(url: string, onOpen: (ws: WebSocket) => void, onMsg: (data: string, recv: number) => void,
                  setStatus: (s: string) => void, pingMs = 0) {
  let backoff = 500;
  let ping: number | undefined;
  const open = () => {
    setStatus("connecting");
    const ws = new WebSocket(url);
    ws.onopen = () => {
      backoff = 500;
      setStatus("live");
      onOpen(ws);
      if (pingMs) ping = window.setInterval(() => ws.readyState === 1 && ws.send("PING"), pingMs);
    };
    ws.onmessage = (e) => onMsg(e.data as string, Date.now());
    ws.onclose = () => {
      clearInterval(ping);
      setStatus("reconnecting");
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 15000);
    };
    ws.onerror = () => ws.close();
  };
  open();
}

function startChainlink() {
  const f = feeds.chainlink;
  connect("wss://ws-live-data.polymarket.com", (ws) => {
    ws.send(JSON.stringify({ action: "subscribe", subscriptions: [
      { topic: "crypto_prices_chainlink", type: "*", filters: "" },
      { topic: "crypto_prices_chainlink", type: "*", filters: JSON.stringify({ symbol: "btc/usd" }) },
    ] }));
  }, (raw, recv) => {
    if (raw.indexOf("btc/usd") < 0) return;
    let p: any;
    try { p = JSON.parse(raw).payload; } catch { return; }
    if (!p || p.symbol !== "btc/usd") return;
    const pts: [number, number][] = Array.isArray(p.data)
      ? p.data.map((x: any) => [Number(x.timestamp), Number(x.value)])
      : p.value !== undefined ? [[Number(p.timestamp || recv), Number(p.value)]] : [];
    pts.sort((a, b) => a[0] - b[0]);
    for (const [ts, v] of pts) {
      if (clTicks.length && ts <= clTicks[clTicks.length - 1][0]) continue;
      clTicks.push([ts, v]);
      if (ts % 1000 === 0) clExact.set(ts / 1000, v);
      if (pts.length === 1) {
        f.push(recv, v, ts);
        for (const id of ["coinbase", "kraken", "binance"] as FeedId[]) {
          const o = feeds[id];
          if (o.last !== null && recv - o.lastRecv < 3000) {
            o.basisObs.push(v - o.last);
            if (o.basisObs.length > 90) o.basisObs.shift();
            const s = [...o.basisObs].sort((a, b) => a - b);
            o.basis = s[Math.floor(s.length / 2)];
          }
        }
      } else {
        // backlog: chart it at its exchange time so the line starts filled
        if (!f.t.length || ts > f.t[f.t.length - 1]) { f.t.push(ts); f.v.push(v); f.last = v; }
      }
    }
    if (clTicks.length > 20000) {
      clTicks.splice(0, 5000);
      const cut = Math.floor(clTicks[0][0] / 1000);
      for (const s of clExact.keys()) if (s < cut) clExact.delete(s);
    }
    bump();
  }, (s) => { f.status = s; bump(); });
}

function startCoinbase() {
  const f = feeds.coinbase;
  connect("wss://ws-feed.exchange.coinbase.com", (ws) => {
    ws.send(JSON.stringify({ type: "subscribe", product_ids: ["BTC-USD"], channels: ["ticker"] }));
  }, (raw, recv) => {
    const d = JSON.parse(raw);
    if (d.type !== "ticker") return;
    f.push(recv, (Number(d.best_bid) + Number(d.best_ask)) / 2, Date.parse(d.time));
  }, (s) => { f.status = s; bump(); });
}

function startKraken() {
  const f = feeds.kraken;
  connect("wss://ws.kraken.com/v2", (ws) => {
    ws.send(JSON.stringify({ method: "subscribe", params: { channel: "trade", symbol: ["BTC/USD"] } }));
  }, (raw, recv) => {
    const d = JSON.parse(raw);
    if (d.channel !== "trade" || !Array.isArray(d.data) || !d.data.length) return;
    const x = d.data[d.data.length - 1];
    f.push(recv, Number(x.price), d.type === "update" ? Date.parse(x.timestamp) : undefined);
  }, (s) => { f.status = s; bump(); });
}

function startBinance() {
  const f = feeds.binance;
  connect("wss://stream.binance.com:9443/ws/btcusdt@trade", () => {}, (raw, recv) => {
    const d = JSON.parse(raw);
    f.push(recv, Number(d.p), Number(d.T));
  }, (s) => { f.status = s; bump(); });
}

// ------------------------------------------------------------------ Polymarket order books
export type Book = { bids: Map<number, number>; asks: Map<number, number>; last: number | null; ts: number; ok: boolean };
export const books = new Map<string, Book>();
const bookSockets = new Map<string, { close: () => void }>(); // slug -> socket
export const bookLat: number[] = [];

function emptyBook(): Book {
  return { bids: new Map(), asks: new Map(), last: null, ts: 0, ok: false };
}

/** Keep one book socket per market slug alive (call with the full set you want; others close). */
export function syncBooks(markets: { slug: string; tokens: Record<string, string> }[]) {
  const want = new Set(markets.map((m) => m.slug));
  for (const [slug, s] of bookSockets) if (!want.has(slug)) { s.close(); bookSockets.delete(slug); }
  for (const m of markets) {
    if (bookSockets.has(m.slug)) continue;
    const toks = Object.values(m.tokens);
    toks.forEach((t) => books.has(t) || books.set(t, emptyBook()));
    let closed = false;
    let ws: WebSocket | null = null;
    let ping: number | undefined;
    const open = () => {
      if (closed) return;
      ws = new WebSocket("wss://ws-subscriptions-clob.polymarket.com/ws/market");
      ws.onopen = () => {
        ws!.send(JSON.stringify({ assets_ids: toks, type: "market" }));
        ping = window.setInterval(() => ws && ws.readyState === 1 && ws.send("PING"), 9000);
      };
      ws.onmessage = (e) => onBook(e.data as string);
      ws.onclose = () => { clearInterval(ping); toks.forEach((t) => { const b = books.get(t); if (b) b.ok = false; }); if (!closed) setTimeout(open, 300); };
      ws.onerror = () => ws && ws.close();
    };
    open();
    bookSockets.set(m.slug, { close: () => { closed = true; ws && ws.close(); toks.forEach((t) => books.delete(t)); } });
  }
}

function onBook(raw: string) {
  if (raw === "PONG") return;
  const recv = Date.now();
  let d: any;
  try { d = JSON.parse(raw); } catch { return; }
  for (const e of Array.isArray(d) ? d : [d]) {
    const et = e.event_type;
    if (e.timestamp) {
      bookLat.push(recv - Number(e.timestamp));
      if (bookLat.length > 400) bookLat.splice(0, 200);
    }
    if (et === "book") {
      const b = books.get(e.asset_id);
      if (!b) continue;
      b.bids = new Map(e.bids.map((x: any) => [Number(x.price), Number(x.size)]));
      b.asks = new Map(e.asks.map((x: any) => [Number(x.price), Number(x.size)]));
      b.ok = true;
      b.ts = recv;
    } else if (et === "price_change") {
      for (const c of e.price_changes || []) {
        const b = books.get(c.asset_id);
        if (!b) continue;
        const side = c.side === "BUY" ? b.bids : b.asks;
        const px = Number(c.price), sz = Number(c.size);
        if (sz <= 0) side.delete(px); else side.set(px, sz);
        b.ts = recv;
      }
    } else if (et === "last_trade_price") {
      const b = books.get(e.asset_id);
      if (b) b.last = Number(e.price);
    } else if (et === "tick_size_change") {
      /* tick is re-read from the market list */
    }
  }
  bump();
}

export function bestBid(b?: Book) {
  if (!b || !b.bids.size) return null;
  let m = -1;
  for (const p of b.bids.keys()) if (p > m) m = p;
  return m;
}
export function bestAsk(b?: Book) {
  if (!b || !b.asks.size) return null;
  let m = 2;
  for (const p of b.asks.keys()) if (p < m) m = p;
  return m;
}
export function levels(side: Map<number, number>, asc: boolean, n: number): [number, number][] {
  return [...side.entries()].sort((a, b) => (asc ? a[0] - b[0] : b[0] - a[0])).slice(0, n);
}

/** Walk the book for a market order. BUY spends `usd`; SELL sells `shares`. */
export function simulate(b: Book | undefined, side: "BUY" | "SELL", qty: number) {
  if (!b) return { shares: 0, usd: 0, avg: null as number | null, worst: null as number | null, filled: false };
  const lv = side === "BUY" ? levels(b.asks, true, 200) : levels(b.bids, false, 200);
  let shares = 0, usd = 0, worst: number | null = null, left = qty;
  for (const [px, sz] of lv) {
    if (left <= 1e-9) break;
    const take = side === "BUY" ? Math.min(sz, left / px) : Math.min(sz, left);
    shares += take;
    usd += take * px;
    left -= side === "BUY" ? take * px : take;
    worst = px;
  }
  return { shares, usd, avg: shares ? usd / shares : null, worst, filled: left <= 1e-6 };
}

let started = false;
export function startFeeds() {
  if (started) return;
  started = true;
  startChainlink();
  startCoinbase();
  startKraken();
  startBinance();
  notifyLoop();
}

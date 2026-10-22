// State of the Trade screen: markets, account (pushed over the trade socket), and feed redraw ticks.
import { useEffect, useMemo, useState } from "react";
import { api } from "../../lib/api";
import { getVersion, startFeeds, subscribe, syncBooks, twapAt } from "../../lib/feeds";
import { tradeSocket } from "../../lib/tradeSocket";

export type Market = {
  slug: string; title: string; start: number; end_ts: number; minutes: number; tick: number; min_size: number;
  tokens: { up: string; down: string }; conditionId: string; neg_risk: boolean; accepting: boolean;
};

export type Account = {
  status: any; orders: any[]; positions: any[]; tokens: Record<string, number>; usdc: number | null;
  trades: any[]; socket: string;
};

/** Re-render at most `hz` times a second, and only when feed data changed. */
export function useFeedTick(hz = 10) {
  const [, set] = useState(0);
  useEffect(() => {
    let last = 0, seen = -1;
    return subscribe(() => {
      const now = performance.now();
      if (now - last < 1000 / hz) return;
      const v = getVersion();
      if (v === seen) return;
      seen = v;
      last = now;
      set(v);
    });
  }, [hz]);
}

export function useTrade() {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [acct, setAcct] = useState<Account>({ status: null, orders: [], positions: [], tokens: {}, usdc: null, trades: [], socket: "connecting" });

  useEffect(() => {
    startFeeds();
    tradeSocket.start();
    const off = tradeSocket.on((m) => {
      setAcct((a) => {
        switch (m.type) {
          case "hello": return { ...a, status: m.status, orders: m.orders, positions: m.positions, usdc: m.status.usdc, tokens: m.status.token_balances || {} };
          case "status": return { ...a, status: m.status };
          case "orders": case "order": return { ...a, orders: m.orders };
          case "positions": return { ...a, positions: m.positions };
          case "balances": return { ...a, usdc: m.usdc, tokens: m.tokens };
          case "trade": return { ...a, trades: [m.trade, ...a.trades.filter((t) => t.id !== m.trade.id)].slice(0, 200) };
          case "socket": return { ...a, socket: m.status };
          default: return a;
        }
      });
      if (m.type === "hello" && m.markets) setMarkets(m.markets);
    });
    const load = () => api.get("/api/trade/markets").then((d) => setMarkets(d.markets)).catch(() => {});
    const loadStatus = () => api.get("/api/trade/status").then((s) => setAcct((a) => ({ ...a, status: s, usdc: s.usdc ?? a.usdc }))).catch(() => {});
    load();
    loadStatus();
    const t1 = setInterval(load, 5000);
    const t2 = setInterval(loadStatus, 3000);
    return () => { off(); clearInterval(t1); clearInterval(t2); };
  }, []);

  // keep one order-book socket per window we may trade (current + next of each length)
  useEffect(() => {
    const now = Date.now() / 1000;
    syncBooks(markets.filter((m) => m.end_ts > now - 5 && m.start < now + 120));
  }, [markets]);

  return { markets, acct, setAcct };
}

export function pickWindows(markets: Market[], minutes: number, now: number) {
  const mine = markets.filter((m) => m.minutes === minutes).sort((a, b) => a.start - b.start);
  const current = mine.find((m) => m.start <= now && now < m.end_ts) || null;
  const next = mine.find((m) => m.start > now) || null;
  return { current, next };
}

/** Price to beat = Chainlink's 60 s TWAP at the window's start (what it settles on). Polymarket's own number
 *  (event metadata) once published - only after the previous window resolved, often minutes in - else our
 *  TWAP from this tab's ticks (needs the minute before the open; a (re)connect replays only ~60 s), else the
 *  server's TWAP from its always-on stream. `now` is the NTP-corrected time. */
export function useStrike(m: Market | null, now: number) {
  const [srv, setSrv] = useState<{ slug: string; open: number; source: string } | null>(null);
  const started = !!m && now >= m.start;
  const local = m && started ? twapAt(m.start) : null;
  const official = !!m && srv?.slug === m.slug && srv.source === "polymarket";
  useEffect(() => {
    if (!m || !started || official) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await api.get(`/api/trade/price-to-beat/${m.slug}`);
        if (!stop && r.open != null) setSrv({ slug: m.slug, open: Number(r.open), source: r.source || "chainlink twap" });
      } catch { /* retry */ }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { stop = true; clearInterval(id); };
  }, [m?.slug, started, official]);
  return useMemo(() => {
    if (!m) return { value: null, src: "" };
    const s = srv && srv.slug === m.slug ? srv : null;
    if (s && s.source === "polymarket") return { value: s.open, src: "Polymarket (60 s TWAP)" };
    if (local !== null) return { value: local, src: "Chainlink 60 s TWAP at open" };
    if (s) return { value: s.open, src: "Chainlink 60 s TWAP at open (server)" };
    return { value: null, src: started ? "waiting for the open" : "opens soon" };
  }, [m, local, srv, started]);
}

// Live view of one trader: their trades as they happen, the current window, books and positions.
import { useMemo } from "react";
import { C, LineChart } from "../../components/charts";
import { Card, Chip, Empty, Kpis, Pill, Switch, Table, toast } from "../../components/ui";
import { api } from "../../lib/api";
import { clock, money, num, tone } from "../../lib/format";
import { useNow, usePoll } from "../../lib/hooks";

export function LiveSection({ t }: { t: any }) {
  const { data: L, reload } = usePoll<any>(`/api/traders/${t.id}/live`, 1000, [t.id]);
  const now = useNow(1000);
  async function toggle(on: boolean) {
    try { await (on ? api.post(`/api/traders/${t.id}/watch`) : api.del(`/api/traders/${t.id}/watch`)); reload(); } catch (e: any) { toast(e.message, "err"); }
  }
  async function auto(on: boolean) {
    try { await api.patch(`/api/traders/${t.id}`, { live_autostart: on }); toast(on ? "Will watch whenever the app runs" : "Autostart off", "ok"); } catch (e: any) { toast(e.message, "err"); }
  }
  const head = (
    <div className="row" style={{ marginBottom: 14 }}>
      {L?.watching ? <button className="btn" onClick={() => toggle(false)}>Stop watching</button> : <button className="btn primary" onClick={() => toggle(true)}>Start watching</button>}
      <Switch on={!!t.live_autostart} onChange={auto} /><span className="t2">Start automatically when the app runs</span>
    </div>
  );
  if (!L) return head;
  if (!L.watching) {
    return (
      <>
        {head}
        <Empty title="Live view is off">Start watching to stream this wallet's trades as they happen (~0.3 s after they match), plus the current window's order book, the Chainlink price it settles on, and open positions. Everything received is stored in the database and included in exports. Their resting orders are not observable: the book is anonymous.</Empty>
      </>
    );
  }
  return <>{head}<LivePanel t={t} L={L} now={now} /></>;
}

function LivePanel({ t, L, now }: { t: any; L: any; now: number }) {
  const h = L.hub, w = L.watch, win = L.window;
  const mine = (L.events as any[]).filter((e) => e.slug === win.slug && e.kind === "TRADE");
  const pos: Record<string, [number, number]> = { up: [0, 0], down: [0, 0] };
  for (const e of mine) {
    const o = String(e.outcome || "").toLowerCase();
    if (o in pos) { const s = e.side === "BUY" ? 1 : -1; pos[o][0] += s * e.size; pos[o][1] += s * e.usdc; }
  }
  const avg = (o: string) => (pos[o][0] > 0 ? pos[o][1] / pos[o][0] : null);
  const last = win.last ? win.last[1] : null;
  const dbps = last && win.strike ? Math.log(last / win.strike) * 1e4 : null;
  const best = (b: any) => (b ? [b.bids[0]?.[0] ?? null, b.asks[0]?.[0] ?? null] : [null, null]);
  const [ub, ua] = best(L.books.up), [db, da] = best(L.books.down);
  const off = h.clock_offset_ms || 0;
  const lastAge = h.last_ms ? (now * 1000 - h.last_ms) / 1000 : null;

  const price = useMemo(() => {
    const pts: [number, number][] = L.prices;
    const x = pts.map((p) => p[0]);
    const at = (s: number) => { let v = null; for (const p of pts) { if (p[0] > s) break; v = p[1]; } return v; };
    const up = mine.filter((e) => String(e.outcome).toLowerCase() === "up").map((e) => [e.ts - win.start, at(e.ts - win.start)]);
    const dn = mine.filter((e) => String(e.outcome).toLowerCase() === "down").map((e) => [e.ts - win.start, at(e.ts - win.start)]);
    const xs = Array.from(new Set([...x, ...up.map((p) => p[0]), ...dn.map((p) => p[0])])).sort((a: any, b: any) => a - b) as number[];
    const m = (arr: any[]) => { const mm = new Map(arr.map((p) => [p[0], p[1]])); return xs.map((v) => (mm.has(v) ? mm.get(v) : null)); };
    return { xs, ys: [m(pts), m(up), m(dn)] };
  }, [L.prices, mine.length, win.start]);

  const fills = useMemo(() => {
    const mu = L.mids.up || [], md = L.mids.down || [];
    const tr = mine.map((e) => [e.ts - win.start, e.price, String(e.outcome).toLowerCase()]);
    const xs = Array.from(new Set([...mu.map((p: any) => p[0]), ...md.map((p: any) => p[0]), ...tr.map((p) => p[0])])).sort((a: any, b: any) => a - b) as number[];
    const ff = (arr: any[]) => { const mm = new Map(arr.map((p) => [p[0], p[1]])); return xs.map((v) => (mm.has(v) ? (mm.get(v) as number) : null)); };
    return { xs, ys: [ff(mu), ff(md), ff(tr.filter((p) => p[2] === "up")), ff(tr.filter((p) => p[2] === "down"))] };
  }, [L.mids, mine.length, win.start]);

  return (
    <>
      <div className="row wrap" style={{ marginBottom: 12 }}>
        <Chip dot={h.rtds === "connected" ? "var(--up)" : "var(--down)"}>Trade feed {h.rtds}</Chip>
        <Chip dot={h.clob === "connected" ? "var(--up)" : h.clob === "idle" ? "var(--warn)" : "var(--down)"}>Order book {h.clob}</Chip>
        <Chip>Watching since {new Date(w.since * 1000).toLocaleTimeString([], { hour12: false })}</Chip>
        {Math.abs(off) > 500 && <Chip dot="var(--warn)">PC clock off by {(off / 1000).toFixed(1)}s (corrected)</Chip>}
        {(h.rtds_error || h.clob_error || w.poll_error) && <span className="muted small">Last error: {h.rtds_error || h.clob_error || w.poll_error}</span>}
      </div>
      <Kpis items={[
        { label: "Feed latency", value: h.feed_p50 !== null ? `${Math.round(h.feed_p50)} ms` : "–", sub: h.feed_p90 !== null ? `p90 ${Math.round(h.feed_p90)} ms` : "measuring", help: "Polymarket server send time to receipt here, NTP-corrected." },
        { label: "Seen after trade", value: w.trade_lag_p50 !== null ? `${(w.trade_lag_p50 / 1000).toFixed(2)}s` : "–", sub: w.trade_lag_n ? `median of ${w.trade_lag_n} trades` : "waiting for a trade" },
        { label: "REST API delay", value: w.poll_behind_p50 !== null ? `${(w.poll_behind_p50 / 1000).toFixed(1)}s` : "–", sub: "slower than websocket" },
        { label: "Captured", value: (w.n_ws + w.n_poll).toLocaleString(), sub: `${w.n_ws} websocket · ${w.n_poll} polled` },
        { label: "Portfolio value", value: money(w.value), sub: `${L.positions.length} positions` },
        { label: "Last message", value: lastAge !== null ? `${lastAge.toFixed(1)}s ago` : "–", sub: `${h.msgs.toLocaleString()} feed messages` },
      ]} />
      <div className="sec">Current window · {win.title}</div>
      <Kpis items={[
        { label: "Time left", value: clock(win.end - now), sub: `${new Date(win.start * 1000).toISOString().slice(11, 16)} to ${new Date(win.end * 1000).toISOString().slice(11, 16)} UTC` },
        { label: `${t.asset} (Chainlink)`, value: num(last, 2), sub: dbps !== null ? `${num(dbps, 1, true)} bps vs strike` : "waiting for price", tone: tone(dbps) },
        { label: "Strike", value: num(win.strike, 2), sub: win.strike_src === "chainlink" ? "Chainlink at open" : win.strike_src === "binance" ? "approx. (Binance at open)" : "unknown" },
        { label: "Up bid / ask", value: ub !== null && ua !== null ? `${ub.toFixed(2)} / ${ua.toFixed(2)}` : "–" },
        { label: "Down bid / ask", value: db !== null && da !== null ? `${db.toFixed(2)} / ${da.toFixed(2)}` : "–" },
        { label: "Their Up", value: `${num(pos.up[0], 1)} sh`, sub: avg("up") ? `avg ${avg("up")!.toFixed(3)}` : "no position" },
        { label: "Their Down", value: `${num(pos.down[0], 1)} sh`, sub: avg("down") ? `avg ${avg("down")!.toFixed(3)}` : "no position" },
        { label: "Their cost", value: money(pos.up[1] + pos.down[1], 2), sub: avg("up") && avg("down") ? `pair cost ${(avg("up")! + avg("down")!).toFixed(3)}` : `${mine.length} fills` },
      ]} />
      <div className="grid2">
        <Card title={`${t.asset}/USD Chainlink (settlement source)`}>
          {price.xs.length ? <LineChart x={price.xs} ys={price.ys} height={290} xRange={[-30, t.window_minutes * 60 + 5]} xFmt={(v) => `${Math.round(v)}s`}
            hlines={win.strike ? [{ y: win.strike, color: "rgba(236,235,232,.55)", label: "strike" }] : []}
            yFmt={(v) => v.toLocaleString("en-US", { maximumFractionDigits: 0 })}
            series={[{ label: "Chainlink", color: C.muted, fmt: (v) => num(v, 2) }, { label: "Up trades", color: C.up, points: true }, { label: "Down trades", color: C.down, points: true }]} />
            : <Empty title="Waiting for prices" />}
        </Card>
        <Card title="Their fills vs market mid">
          <LineChart x={fills.xs} ys={fills.ys} height={290} xRange={[-5, t.window_minutes * 60 + 5]} yRange={[0, 1]} xFmt={(v) => `${Math.round(v)}s`} yFmt={(v) => v.toFixed(1)}
            series={[{ label: "Up mid", color: C.up, width: 1.5, step: true }, { label: "Down mid", color: C.down, width: 1.5, step: true },
                     { label: "Up fills", color: C.up, points: true }, { label: "Down fills", color: C.down, points: true }]} />
        </Card>
      </div>
      <div className="grid-books">
        <Card title="Up book"><Ladder b={L.books.up} /></Card>
        <Card title="Down book"><Ladder b={L.books.down} /></Card>
        <Card title="Event feed">
          <Table rows={[...L.events].reverse().slice(0, 300)} maxHeight={330} cols={[
            { key: "recv_ms", label: "Seen", align: "l", render: (e: any) => new Date(e.recv_ms).toLocaleTimeString([], { hour12: false }) + "." + String(Math.floor(e.recv_ms % 1000)).padStart(3, "0") },
            { key: "delay", label: "Delay s", render: (e: any) => (e.source === "history" || !e.ts ? "–" : num((e.recv_ms + off) / 1000 - e.ts, 2)) },
            { key: "source", label: "Via", render: (e: any) => ({ ws: "websocket", poll: "api poll", history: "history" } as any)[e.source] },
            { key: "kind", label: "Type" },
            { key: "slug", label: "Market", render: (e: any) => String(e.slug || e.title || "").replace("-updown-", " ").slice(0, 28) },
            { key: "outcome", label: "Outcome", render: (e: any) => e.outcome ? <Pill side={String(e.outcome).toLowerCase()} /> : "–" },
            { key: "side", label: "Side" },
            { key: "price", label: "Price", render: (e: any) => num(e.price, 3) },
            { key: "size", label: "Shares", render: (e: any) => num(e.size, 2) },
            { key: "usdc", label: "USD", render: (e: any) => money(e.usdc, 2) },
          ]} />
        </Card>
      </div>
      <div className="sec">Open positions (Polymarket positions API, every 15 s)</div>
      <div className="card flush">
        <Table rows={L.positions} empty={<p className="hint" style={{ padding: 14 }}>No open positions reported (or not loaded yet).</p>} cols={[
          { key: "title", label: "Market", align: "l", render: (r: any) => String(r.title || "").slice(0, 60) },
          { key: "outcome", label: "Outcome" }, { key: "size", label: "Shares", render: (r: any) => num(r.size, 2) },
          { key: "avgPrice", label: "Avg", render: (r: any) => num(r.avgPrice, 3) }, { key: "curPrice", label: "Now", render: (r: any) => num(r.curPrice, 3) },
          { key: "currentValue", label: "Value", render: (r: any) => money(r.currentValue, 2) },
          { key: "cashPnl", label: "PnL", render: (r: any) => <span className={tone(r.cashPnl)}>{money(r.cashPnl, 2)}</span> },
        ]} />
      </div>
    </>
  );
}

function Ladder({ b }: { b: any }) {
  if (!b) return <p className="hint">No book data yet.</p>;
  const asks = b.asks.slice(0, 8).reverse(), bids = b.bids.slice(0, 8);
  return (
    <table className="ladder">
      <tbody>
        {asks.map(([p, s]: number[]) => <tr key={"a" + p}><td className="ask">{p.toFixed(2)}</td><td>{Math.round(s).toLocaleString()}</td></tr>)}
        {asks.length > 0 && bids.length > 0 && <tr className="spread"><td colSpan={2}>spread {(b.asks[0][0] - b.bids[0][0]).toFixed(2)}</td></tr>}
        {bids.map(([p, s]: number[]) => <tr key={"b" + p}><td className="bid">{p.toFixed(2)}</td><td>{Math.round(s).toLocaleString()}</td></tr>)}
      </tbody>
    </table>
  );
}

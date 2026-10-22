// Positions, open orders (cancel), fills and order log of the live account.
import { useState } from "react";
import { Empty, Pill, Table, Tabs, toast } from "../../components/ui";
import { cents, hms, money, num, pct, signed, tone } from "../../lib/format";
import { usePoll } from "../../lib/hooks";
import { tradeSocket } from "../../lib/tradeSocket";
import { livePosition } from "./positions";
import { Account, Market } from "./useTrade";

const TABS = ["Positions", "Open orders", "Fills", "Order log"] as const;

export function AccountPanel({ acct, markets, now }: { acct: Account; markets: Market[]; now: number }) {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Positions");
  const hist = usePoll<any>(tab === "Order log" || tab === "Fills" ? "/api/trade/history" : null, 5000, [tab]);
  const tokenInfo = new Map<string, { m: Market; o: "up" | "down" }>();
  markets.forEach((m) => { tokenInfo.set(m.tokens.up, { m, o: "up" }); tokenInfo.set(m.tokens.down, { m, o: "down" }); });

  // positions with live P/L; shares that are in the wallet but not yet in Polymarket's list show up as well
  const listed = new Set(acct.positions.map((p) => String(p.asset)));
  const rows = [
    ...acct.positions.map((p) => {
      const tok = String(p.asset), info = tokenInfo.get(tok);
      const live = info && now < info.m.end_ts;            // book still trading: value at the live bid
      const lp = livePosition(acct, tok, Number(p.size) || 0);
      const value = live && lp.value !== null ? lp.value : Number(p.currentValue);
      const cost = Number(p.initialValue) || (lp.cost ?? 0);
      return { tok, title: p.title, outcome: String(p.outcome).toLowerCase(), size: Number(p.size), avg: lp.avg, px: live ? lp.px : Number(p.curPrice),
               value, cost, pnl: value - cost, pct: cost ? (value - cost) / cost : null, live: !!live, redeemable: !!p.redeemable };
    }),
    ...Object.entries(acct.tokens).filter(([tok, v]) => v >= 0.01 && !listed.has(tok)).map(([tok, v]) => {
      const info = tokenInfo.get(tok), lp = livePosition(acct, tok, v);
      return { tok, title: info ? `BTC ${info.m.minutes}m · ${hms(info.m.start).slice(0, 5)} (just filled)` : tok.slice(0, 10) + "…",
               outcome: info?.o || "", size: v, avg: null, px: lp.px, value: lp.value ?? 0, cost: null as number | null, pnl: null as number | null,
               pct: null as number | null, live: true, redeemable: false };
    }),
  ];
  const priced = rows.filter((r) => r.pnl !== null);
  const tot = { value: rows.reduce((s, r) => s + (r.value || 0), 0), cost: priced.reduce((s, r) => s + (r.cost || 0), 0),
                pnl: priced.reduce((s, r) => s + (r.pnl || 0), 0) };

  async function cancel(id: string) {
    const r = await tradeSocket.request("cancel", { id });
    toast(r.ok ? "Cancel sent" : `Cancel failed: ${r.error}`, r.ok ? "ok" : "err");
  }
  async function cancelAll() {
    const r = await tradeSocket.request("cancel_all");
    toast(r.ok ? "All orders cancelled" : `Cancel failed: ${r.error}`, r.ok ? "ok" : "err");
  }

  return (
    <div className="card">
      <Tabs value={tab} options={TABS} onChange={setTab} counts={{ "Open orders": acct.orders.length, Positions: rows.length }} />
      {tab === "Positions" && (
        <>
          {rows.length > 0 && (
            <div className="pos-sum">
              <span>Positions <b>{money(tot.value, 2)}</b></span>
              {priced.length > 0 && <span className="muted">cost {money(tot.cost, 2)}</span>}
              {priced.length > 0 && <span className={tone(tot.pnl)}><b>{signed(tot.pnl)}</b> ({pct(tot.cost ? tot.pnl / tot.cost : null, 1, true)})</span>}
              <span className="spacer" />
              <span className="muted small">open windows valued at the live best bid</span>
            </div>
          )}
          <Table rows={rows} empty={<Empty title="No positions">Positions appear here as soon as an order fills.</Empty>} cols={[
            { key: "title", label: "Market", align: "l", render: (r: any) => <span title={r.title}>{String(r.title || "").slice(0, 48)}</span> },
            { key: "outcome", label: "Outcome", render: (r: any) => r.outcome ? <Pill side={r.outcome} /> : "–" },
            { key: "size", label: "Shares", render: (r: any) => num(r.size, 2) },
            { key: "avg", label: "Avg → now", render: (r: any) => `${cents(r.avg)} → ${cents(r.px)}${r.live ? "" : r.redeemable ? " · redeem" : ""}` },
            { key: "value", label: "Value", render: (r: any) => money(r.value, 2) },
            { key: "pnl", label: "P/L", render: (r: any) => r.pnl === null ? <span className="muted">pending</span>
                : <span className={tone(r.pnl)}>{signed(r.pnl)} <span className="small">({pct(r.pct, 1, true)})</span></span> },
          ]} />
        </>
      )}
      {tab === "Open orders" && (
        <>
          {acct.orders.length > 0 && <div className="row" style={{ marginBottom: 8 }}><span className="spacer" /><button className="btn danger sm" onClick={cancelAll}>Cancel all</button></div>}
          <Table rows={acct.orders} empty={<Empty title="No open orders">Resting limit orders show here with a cancel button.</Empty>} cols={[
            { key: "market", label: "Window", align: "l", render: (r: any) => { const i = tokenInfo.get(r.asset_id); return i ? `BTC ${i.m.minutes}m · ${hms(i.m.start).slice(0, 5)}` : String(r.market || "").slice(0, 10) + "…"; } },
            { key: "side", label: "Side", render: (r: any) => <><Pill side={String(r.outcome || tokenInfo.get(r.asset_id)?.o || "").toLowerCase()} />{r.side}</> },
            { key: "price", label: "Price", render: (r: any) => cents(Number(r.price)) },
            { key: "filled", label: "Filled", render: (r: any) => `${num(Number(r.size_matched || 0), 1)} / ${num(Number(r.original_size || 0), 1)}` },
            { key: "x", label: "", render: (r: any) => <button className="btn ghost sm" onClick={() => cancel(r.id)}>Cancel</button> },
          ]} />
        </>
      )}
      {tab === "Fills" && (
        <Table rows={[...acct.trades, ...((hist.data?.trades || []).filter((t: any) => !acct.trades.some((x) => x.id === t.id)))]}
          empty={<Empty title="No fills yet" />} cols={[
            { key: "ts", label: "Time", align: "l", render: (r: any) => hms(Number(r.ts || r.match_time || r.timestamp || Date.now() / 1000)) },
            { key: "outcome", label: "Outcome", render: (r: any) => <Pill side={String(r.outcome).toLowerCase()} /> },
            { key: "side", label: "Side" },
            { key: "price", label: "Price", render: (r: any) => cents(Number(r.price)) },
            { key: "size", label: "Shares", render: (r: any) => num(Number(r.size), 2) },
            { key: "status", label: "Status", render: (r: any) => <span className="muted">{r.status}</span> },
          ]} />
      )}
      {tab === "Order log" && (
        <Table rows={hist.data?.orders || []} empty={<Empty title="No orders sent from this app yet" />} cols={[
          { key: "ts", label: "Time", align: "l", render: (r: any) => hms(r.ts) },
          { key: "slug", label: "Window", render: (r: any) => String(r.slug || "").replace("btc-updown-", "") },
          { key: "side", label: "Order", render: (r: any) => <><Pill side={r.outcome} />{r.side} {r.order_type}</> },
          { key: "price", label: "Price", render: (r: any) => cents(r.price) },
          { key: "amount", label: "Size", render: (r: any) => r.amount ? money(r.amount, 2) : `${num(r.size, 1)} sh` },
          { key: "status", label: "Status", render: (r: any) => <span className={r.error ? "dn" : ""} title={r.error || ""}>{r.error ? "rejected" : r.status}</span> },
          { key: "latency_ms", label: "Server ms", render: (r: any) => num(r.latency_ms, 0) },
        ]} />
      )}
    </div>
  );
}

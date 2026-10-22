// Order ticket (Polymarket layout): Buy/Sell, Market/Limit, Up/Down, amount -> one click to the CLOB.
import { useEffect, useState } from "react";
import { Switch, toast } from "../../components/ui";
import { api } from "../../lib/api";
import { bestAsk, bestBid, books, simulate } from "../../lib/feeds";
import { cents, money, num, pct, signed, tone } from "../../lib/format";
import { usePref } from "../../lib/hooks";
import { tradeSocket } from "../../lib/tradeSocket";
import { floor2, heldShares, livePosition } from "./positions";
import { Account, Market, useFeedTick } from "./useTrade";

const roundTick = (p: number, tick: number) => Math.round(p / tick) * tick;

export type SellRequest = { outcome: "up" | "down"; shares: number; n: number };

export function OrderTicket({ m, acct, now, outcome, setOutcome, limitPx, setLimitPx, sellReq }: {
  m: Market | null; acct: Account; now: number; outcome: "up" | "down"; setOutcome: (o: "up" | "down") => void;
  limitPx: number | null; setLimitPx: (p: number | null) => void; sellReq?: SellRequest | null;
}) {
  useFeedTick(6);
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [type, setType] = usePref<"market" | "limit">("ticket.type", "market");
  const [amount, setAmount] = useState("10");
  const [shares, setShares] = useState("10");
  const [slip, setSlip] = usePref<number>("ticket.slip", 0.02);
  const [postOnly, setPostOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<any>(null);
  const st = acct.status;

  useEffect(() => { if (limitPx !== null) setType("limit"); }, [limitPx]);
  // "Sell" on a position: load the ticket with a market sell of the whole position (you still press the button)
  useEffect(() => {
    if (!sellReq) return;
    setSide("SELL"); setType("market"); setLimitPx(null); setOutcome(sellReq.outcome); setShares(String(sellReq.shares));
  }, [sellReq?.n]);

  const tick = m?.tick || 0.01;
  const token = m ? m.tokens[outcome] : undefined;
  const b = token ? books.get(token) : undefined;
  const px = { up: m ? (side === "BUY" ? bestAsk(books.get(m.tokens.up)) : bestBid(books.get(m.tokens.up))) : null,
               down: m ? (side === "BUY" ? bestAsk(books.get(m.tokens.down)) : bestBid(books.get(m.tokens.down))) : null };
  const held = heldShares(acct, token);
  const pos = token && held >= 0.01 ? livePosition(acct, token, held) : null;
  const sideName = outcome === "up" ? "Up" : "Down";

  // Selling: start from what is actually held (the default 10 shares is usually more than that)
  useEffect(() => {
    const cur = Number(shares) || 0;
    if (side === "SELL" && held >= 0.01 && (cur <= 0 || cur > held + 1e-6)) setShares(String(floor2(held)));
  }, [side, token, held]);

  const amt = Number(amount) || 0, sh = Number(shares) || 0;
  const lp = limitPx ?? (side === "BUY" ? bestBid(b) : bestAsk(b)) ?? 0.5;
  const sim = type === "market" ? simulate(b, side, side === "BUY" ? amt : sh) : null;
  let sendPx: number | null = null;
  if (type === "market" && sim?.worst !== null && sim?.worst !== undefined) {
    sendPx = side === "BUY" ? Math.min(1 - tick, roundTick(sim.worst + slip, tick)) : Math.max(tick, roundTick(sim.worst - slip, tick));
  }
  const est = type === "market"
    ? { shares: sim?.shares || 0, cost: sim?.usd || 0, avg: sim?.avg ?? null }
    : { shares: sh, cost: sh * lp, avg: lp };
  const toWin = side === "BUY" ? est.shares : est.cost;

  const problems: string[] = [];
  if (!st?.enabled) problems.push(st?.reason || "Trading gateway is starting…");
  else if (!st.armed) problems.push("Arm trading to send orders.");
  if (!m) problems.push("No window selected.");
  else if (now > m.end_ts) problems.push("This window has closed.");
  // every way the button can be disabled gets a reason here (it used to go dark silently)
  if (side === "SELL" && held < 0.01)
    problems.push(held > 1e-6 ? `Only ${num(held, 4)} ${sideName} shares left (fee dust): too small to sell.`
                              : `You hold no ${sideName} shares in this window. After a buy they show up within a few seconds.`);
  else if (side === "SELL" && sh > held + 1e-6) problems.push(`You hold ${num(held, 2)} ${sideName} shares.`);
  if ((side === "SELL" || type === "limit") && sh <= 0) problems.push("Enter the number of shares.");
  if (type === "market" && side === "BUY" && amt < 1) problems.push("Minimum market buy is $1.");
  if (type === "market" && sim && sim.worst === null && (side === "BUY" ? amt : sh) > 0)
    problems.push(side === "BUY" ? `No ${sideName} asks in the book right now.` : `No ${sideName} bids in the book right now: nobody to sell to.`);
  else if (type === "market" && sim && !sim.filled && (side === "BUY" ? amt : sh) > 0) problems.push("Not enough liquidity in the book for the full amount.");
  if (type === "limit" && m && sh > 0 && sh < m.min_size) problems.push(`Minimum ${m.min_size} shares for a limit order.`);

  async function arm(on: boolean) {
    try {
      await api.post("/api/trade/arm", { armed: on });
      toast(on ? "Trading ARMED: orders go to Polymarket with real money." : "Trading disarmed.", on ? "err" : "ok");
    } catch (e: any) { toast(e.message, "err"); }
  }

  async function send(body: Record<string, unknown>, label: string) {
    setBusy(true);
    const r = await tradeSocket.request("order", body);
    setBusy(false);
    const res = r.result || {};
    setLast({ ...res, client_ms: r.client_ms, ok: r.ok && res.ok, error: r.error || res.error });
    if (r.ok && res.ok) toast(<span><b>{label}</b> · {res.status} · {Math.round(r.client_ms)} ms</span>, "ok");
    else toast(<span><b>Order rejected</b><br />{r.error || res.error}</span>, "err", 7000);
  }

  async function submit() {
    if (!m || busy) return;
    const body = type === "market"
      ? { slug: m.slug, outcome, side, type, amount: amt, shares: sh, price: sendPx }
      : { slug: m.slug, outcome, side, type, shares: sh, price: roundTick(lp, tick), post_only: postOnly };
    await send(body, `${side === "BUY" ? "Bought" : "Sold"} ${outcome === "up" ? "Up" : "Down"}`);
  }

  /** One tap: market-buy `usd` of outcome `o` with the current slippage limit. */
  async function quickBuy(o: "up" | "down", usd: number) {
    if (!m || busy) return;
    if (!st?.enabled || !st.armed) { toast(st?.enabled ? "Switch on Live trading first." : st?.reason || "Trading unavailable", "err"); return; }
    const s = simulate(books.get(m.tokens[o]), "BUY", usd);
    if (s.worst === null || !s.filled) { toast(`Not enough ${o === "up" ? "Up" : "Down"} liquidity for $${usd}.`, "err"); return; }
    const price = Math.min(1 - tick, roundTick(s.worst + slip, tick));
    await send({ slug: m.slug, outcome: o, side: "BUY", type: "market", amount: usd, price }, `Bought $${usd} ${o === "up" ? "Up" : "Down"}`);
  }

  async function setupApprovals() {
    if (!window.confirm("Send the missing trading approvals for your wallet? This is an on-chain transaction (gasless, via the Polymarket relayer).")) return;
    try {
      const r = await api.post("/api/trade/setup-approvals");
      toast(r?.complete ? "Trading approvals complete" : `Approvals sent; ${r?.missing ?? "?"} still missing`, r?.complete ? "ok" : "err");
    } catch (e: any) { toast(e.message, "err"); }
  }

  return (
    <div className="card ticket">
      <div className="row">
        <div className="seg">
          {(["BUY", "SELL"] as const).map((s) => <button key={s} className={side === s ? "on" : ""} onClick={() => setSide(s)}>{s === "BUY" ? "Buy" : "Sell"}</button>)}
        </div>
        <span className="spacer" />
        <select className="input" style={{ width: 110, height: 32 }} value={type} onChange={(e) => { setType(e.target.value as any); if (e.target.value === "market") setLimitPx(null); }}>
          <option value="market">Market</option>
          <option value="limit">Limit</option>
        </select>
      </div>

      <QuickBuy prices={px} disabled={busy || !m} onBuy={quickBuy} side={side} />

      <div className="sides">
        {(["up", "down"] as const).map((o) => {
          const h = m ? heldShares(acct, m.tokens[o]) : 0;
          return (
            <div key={o} className="side-col">
              <button className={`side-btn ${o === "up" ? "is-up" : "is-dn"}${outcome === o ? " on" : ""}`} onClick={() => setOutcome(o)}>
                {o === "up" ? "Up" : "Down"} {cents(px[o])}
              </button>
              {side === "SELL" && <span className={`side-held ${h >= 0.01 ? (o === "up" ? "up" : "dn") : "muted"}`}>{num(h, 2)} shares</span>}
            </div>
          );
        })}
      </div>

      {pos && (
        <div className="pos-track" title="Value at the best bid now (what selling would get at the top of the book) vs your average buy price">
          <span>Your {sideName}: <b>{num(pos.shares, 2)} sh</b>{pos.avg !== null && <> · avg {cents(pos.avg)}</>} · bid {cents(pos.px)}</span>
          <span className={tone(pos.pnl)}>{pos.pnl !== null ? <>{signed(pos.pnl)} ({pct(pos.pct, 1, true)})</> : "cost pending"}</span>
        </div>
      )}

      {type === "limit" && (
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="t2" style={{ width: 90 }}>Limit price</span>
          <div className="stepper" style={{ flex: 1 }}>
            <button onClick={() => setLimitPx(Math.max(tick, roundTick(lp - tick, tick)))}>−</button>
            <input value={(lp * 100).toFixed(tick < 0.01 ? 1 : 0)} onChange={(e) => setLimitPx(Math.min(1 - tick, Math.max(tick, (Number(e.target.value) || 0) / 100)))} />
            <button onClick={() => setLimitPx(Math.min(1 - tick, roundTick(lp + tick, tick)))}>+</button>
          </div>
          <span className="muted">¢</span>
        </div>
      )}

      {type === "market" && side === "BUY" ? (
        <>
          <div className="amount"><span className="t2">Amount</span>
            <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} aria-label="Amount in dollars" />
          </div>
          <div className="quick">
            {[1, 5, 10, 25, 100].map((v) => <button key={v} onClick={() => setAmount(String((Number(amount) || 0) + v))}>+${v}</button>)}
            <button onClick={() => acct.usdc && setAmount(String(Math.floor(Math.min(acct.usdc, st?.max_order_usd || 1e9) * 100) / 100))}>Max</button>
          </div>
        </>
      ) : (
        <>
          <div className="amount"><span className="t2">Shares</span>
            <input inputMode="decimal" value={shares} onChange={(e) => setShares(e.target.value.replace(/[^0-9.]/g, ""))} aria-label="Shares" />
          </div>
          <div className="quick">
            {side === "SELL"
              ? <>{[0.25, 0.5, 0.75].map((f) => <button key={f} disabled={held < 0.01} onClick={() => setShares(String(floor2(held * f)))}>{f * 100}%</button>)}
                  <button disabled={held < 0.01} onClick={() => setShares(String(floor2(held)))}>Max</button></>
              : [5, 10, 25, 100].map((v) => <button key={v} onClick={() => setShares(String((Number(shares) || 0) + v))}>+{v}</button>)}
          </div>
        </>
      )}

      {side === "SELL" && (
        <div className="receive">
          <div><div className="receive-l">You'll receive</div><div className="muted small">Avg. price {cents(est.avg)}</div></div>
          <div className="receive-v">{money(est.cost, 2)}</div>
        </div>
      )}

      <div className="ticket-rows">
        {side === "BUY" && <>
          <span className="l">Avg price</span><span className="v">{cents(est.avg)}</span>
          <span className="l">Shares</span><span className="v">{num(est.shares, 2)}</span>
          <span className="l">To win</span><span className="v up">{money(toWin, 2)}</span>
        </>}
        {type === "market" && <>
          <span className="l" title="Worst price sent with the order (fill-and-kill): your price limit">Price limit</span>
          <span className="v">{cents(sendPx)} <select className="input" style={{ height: 24, width: 74, display: "inline", padding: "0 4px", marginLeft: 6 }} value={slip} onChange={(e) => setSlip(Number(e.target.value))} title="Slippage allowed beyond the book's worst level">
            {[0, 0.01, 0.02, 0.03, 0.05, 0.1].map((s) => <option key={s} value={s}>+{Math.round(s * 100)}¢</option>)}
          </select></span>
        </>}
        {type === "limit" && <>
          <span className="l">Post only (maker)</span>
          <span className="v"><input type="checkbox" checked={postOnly} onChange={(e) => setPostOnly(e.target.checked)} /></span>
        </>}
      </div>

      <button className={`go ${side === "SELL" ? "" : outcome === "up" ? "is-up" : "is-dn"}`} disabled={busy || problems.length > 0 || (type === "market" && sendPx === null)} onClick={submit}>
        {busy ? "Sending…" : `${side === "BUY" ? "Buy" : "Sell"} ${outcome === "up" ? "Up" : "Down"}`}
      </button>
      {problems.length > 0 && <p className="hint" style={{ marginTop: 8 }}>{problems[0]}</p>}

      <div className="row" style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
        <div>
          <div style={{ fontWeight: 650 }}>Live trading</div>
          <div className="muted small">{st?.enabled ? `${money(acct.usdc, 2)} USDC · max ${money(st.max_order_usd)}/order` : "unavailable"}</div>
          {st?.enabled && (
            <div className="muted small" title={st.wallet}>
              {({ DEPOSIT_WALLET: "Deposit wallet", POLY_PROXY: "Proxy wallet", GNOSIS_SAFE: "Safe wallet", EOA: "EOA wallet" } as Record<string, string>)[st.wallet_type] || st.wallet_type}
              {st.approvals && !st.approvals.complete && <> · {st.approvals.missing} approvals missing <button className="btn ghost sm" style={{ height: 20, padding: "0 6px" }} onClick={setupApprovals}>Set up</button></>}
            </div>
          )}
        </div>
        <span className="spacer" />
        <span className={st?.armed ? "dn small" : "muted small"} style={{ fontWeight: 650 }}>{st?.armed ? "ARMED" : "Safe"}</span>
        <Switch on={!!st?.armed} danger onChange={arm} title="Orders are refused until armed. Resets when the server restarts." />
      </div>
      {last && (
        <div className="small muted" style={{ marginTop: 10 }}>
          Last order: <b className={last.ok ? "up" : "dn"}>{last.ok ? last.status : "rejected"}</b>
          {last.client_ms !== undefined && <> · click→ack {Math.round(last.client_ms)} ms</>}
          {last.sign_ms !== undefined && <> (sign {last.sign_ms} · CLOB {last.post_ms} ms)</>}
          {last.error && <div className="dn">{last.error}</div>}
        </div>
      )}
    </div>
  );
}

/** One-tap market buys: fixed $ amounts for Up and Down. Amounts are editable (pencil), kept in this browser. */
function QuickBuy({ prices, disabled, onBuy, side }: {
  prices: { up: number | null; down: number | null }; disabled: boolean; onBuy: (o: "up" | "down", usd: number) => void; side: string;
}) {
  const [amts, setAmts] = usePref<number[]>("ticket.quick", [1, 5, 10]);
  const [edit, setEdit] = useState(false);
  const [draft, setDraft] = useState(amts.map(String));
  const save = () => {
    setAmts(draft.map((x) => Math.max(1, Math.round((Number(x) || 1) * 100) / 100)));
    setEdit(false);
  };
  return (
    <div className="qb">
      <div className="row" style={{ marginBottom: 6 }}>
        <span className="t2 small" style={{ fontWeight: 650 }}>One-tap buy</span>
        <span className="muted small">market, sent instantly{side === "SELL" ? " (always buys)" : ""}</span>
        <span className="spacer" />
        {edit
          ? <button className="btn ghost sm" onClick={save}>Save</button>
          : <button className="btn ghost sm" title="Edit amounts" onClick={() => { setDraft(amts.map(String)); setEdit(true); }}>✎</button>}
      </div>
      {edit ? (
        <div className="row">
          {draft.map((d, i) => (
            <input key={i} className="input" style={{ height: 32, textAlign: "center" }} inputMode="decimal" value={d}
                   onChange={(e) => setDraft(draft.map((x, k) => (k === i ? e.target.value.replace(/[^0-9.]/g, "") : x)))}
                   onKeyDown={(e) => e.key === "Enter" && save()} aria-label={`One-tap amount ${i + 1}`} />
          ))}
        </div>
      ) : (
        (["up", "down"] as const).map((o) => (
          <div key={o} className="qb-row">
            <span className={`qb-label ${o === "up" ? "is-up" : "is-dn"}`}>{o === "up" ? "Up" : "Down"} <span className="muted">{cents(prices[o])}</span></span>
            {amts.map((a, i) => (
              <button key={i} className={`qb-btn ${o === "up" ? "is-up" : "is-dn"}`} disabled={disabled} onClick={() => onBuy(o, a)}>${a}</button>
            ))}
          </div>
        ))
      )}
    </div>
  );
}

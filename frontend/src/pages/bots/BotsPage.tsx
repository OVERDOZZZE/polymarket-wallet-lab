// Bots: every bot as a portfolio card; create new ones from a strategy.
import { MouseEvent, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Sparkline } from "../../components/Sparkline";
import { Empty, Field, LiveBadge, Modal, Table, toast } from "../../components/ui";
import { api } from "../../lib/api";
import { dtUTC, money, pct, signed, tone } from "../../lib/format";
import { usePoll, usePref } from "../../lib/hooks";

export default function BotsPage() {
  const { data, reload } = usePoll<any[]>("/api/bots", 3000);
  const [adding, setAdding] = useState(false);
  const [showArchived, setShowArchived] = usePref("bots.showArchived", false);
  const nav = useNavigate();
  const active = (data || []).filter((b) => !b.archived), archived = (data || []).filter((b) => b.archived);
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Bots</h1>
          <p className="page-sub">Each bot runs in its own process with its own paper account. Everything it does is saved.</p>
        </div>
        <span className="spacer" />
        <button className="btn primary" onClick={() => setAdding(true)}>New bot</button>
      </div>
      {active.length > 0 && <BotTotals bots={active} />}
      {data && !data.length && <Empty title="No bots yet">Create one from a strategy. It starts on paper with live Polymarket data.</Empty>}
      <div className="grid3">
        {active.map((b) => (
          <div key={b.id} className="card" style={{ cursor: "pointer" }} onClick={() => nav(`/bots/${b.id}`)}>
            <div className="pm-prof" style={{ marginBottom: 10 }}>
              <div className={`pm-ava ${b.live ? "live" : "b"}`} style={{ width: 46, height: 46 }} />
              <div>
                <div className="pm-name" style={{ fontSize: "1.1rem" }}>{b.name}<LiveBadge on={b.running} /></div>
                <div className="pm-meta">{b.strategy_label} · {b.live ? <b className="dn">REAL MONEY{b.live.halted ? " · halted" : b.live.paused ? " · paused" : ""}</b> : b.mode} · account #{b.account}</div>
              </div>
            </div>
            <div className="bot-card-body">
              {b.live ? (
                <div className="pm-rows">
                  <span className="l">Wallet</span><span className="v">{b.live.usdc == null ? "–" : money(b.live.usdc, 2)}</span>
                  <span className="l">Live PnL</span><span className={`v ${tone(b.live.realized)}`}>{signed(b.live.realized)}</span>
                  <span className="l">At risk</span><span className="v">{money(b.live.at_risk, 2)}</span>
                  <span className="l">Limit left</span><span className={`v ${b.live.room < 5 ? "dn" : ""}`} title={`of the ${money(b.live.loss_limit, 0)} live loss limit`}>{money(Math.max(0, b.live.room), 2)}</span>
                  <span className="l">Live windows</span><span className="v">{b.live.windows_done ? `${b.live.windows_won}/${b.live.windows_done}` : "–"}</span>
                </div>
              ) : (
              <div className="pm-rows">
                <span className="l">Portfolio</span><span className="v">{money(b.portfolio, 2)}</span>
                <span className="l">PnL today</span><span className={`v ${tone(b.pnl_today)}`}>{signed(b.pnl_today)}</span>
                <span className="l">Total PnL</span><span className={`v ${tone(b.pnl_total)}`}>{signed(b.pnl_total)}</span>
                <span className="l">Rebates (est.)</span><span className="v">{money(b.rebates, 2)}</span>
                <span className="l">Windows traded</span><span className="v">{b.windows ?? "–"}</span>
              </div>
              )}
              <Sparkline points={b.spark || []} height={76} label={b.live ? "Live PnL (real) · 7 d" : "Portfolio · 7 d"} />
            </div>
          </div>
        ))}
      </div>
      {archived.length > 0 && (
        <div className="col" style={{ marginTop: 18 }}>
          <div className="row">
            <button className="btn ghost sm" onClick={() => setShowArchived(!showArchived)}>
              {showArchived ? "Hide" : "Show"} archived bots ({archived.length})
            </button>
          </div>
          {showArchived && <ArchivedBots bots={archived} onChange={reload} />}
        </div>
      )}
      {adding && <NewBot onClose={() => setAdding(false)} onDone={(id) => { reload(); nav(`/bots/${id}`); }} />}
    </div>
  );
}

// Archived bots: stopped and kept out of the cards and totals; one compact row each, data untouched.
function ArchivedBots({ bots, onChange }: { bots: any[]; onChange: () => void }) {
  const nav = useNavigate();
  async function unarchive(e: MouseEvent, b: any) {
    e.stopPropagation();
    try { await api.post(`/api/bots/${b.id}/unarchive`); toast(`${b.name} unarchived (not started)`, "ok"); onChange(); }
    catch (err: any) { toast(err.message, "err"); }
  }
  return (
    <div className="card flush">
      <Table rows={bots} onRow={(b: any) => nav(`/bots/${b.id}`)} initialSort={{ key: "archived", desc: true }} cols={[
        { key: "name", label: "Bot", align: "l", render: (b: any) => <><b>{b.name}</b> <span className="muted small">{b.id}</span></> },
        { key: "strategy_label", label: "Strategy", align: "l" },
        { key: "pnl_total", label: "Total PnL", render: (b: any) => { const v = b.live ? b.live.realized : b.pnl_total; return <span className={tone(v)}>{v == null ? "–" : signed(v)}</span>; },
          sort: (b: any) => (b.live ? b.live.realized : b.pnl_total) ?? null },
        { key: "windows", label: "Windows", render: (b: any) => b.windows ?? "–" },
        { key: "archived", label: "Archived (UTC)", render: (b: any) => dtUTC(b.archived) },
        { key: "act", label: "", render: (b: any) => <button className="btn sm" onClick={(e) => unarchive(e, b)}>Unarchive</button> },
      ]} />
    </div>
  );
}

// Totals across every paper bot's current account (stopped bots included: their last state counts; archived bots
// are not passed in). Live bots are left out: their paper twin only sizes real orders and would double-count the
// paper bot it copies.
function BotTotals({ bots }: { bots: any[] }) {
  const paper = bots.filter((b) => !b.live);
  const sum = (k: string) => paper.reduce((a, b) => a + (Number.isFinite(b[k]) ? b[k] : 0), 0);
  const running = bots.filter((b) => b.running).length;
  const bankroll = sum("bankroll"), pnl = sum("pnl_total"), today = sum("pnl_today");
  const fills = sum("fills"), maker = sum("maker_fills"), taker = sum("taker_fills");
  const done = sum("windows_done"), won = sum("windows_won");
  const pnlPct = bankroll ? pnl / bankroll : null;
  const winRate = done ? won / done : null;
  const best = paper.reduce((m, b) => (Number.isFinite(b.pnl_total) && (!m || b.pnl_total > m.pnl_total) ? b : m), null as any);
  const int = (x: number) => Math.round(x).toLocaleString("en-US");
  return (
    <div className="bt-grid">
      <div className={`bt-card bt-hero ${tone(pnl)}`} style={{ "--i": 0 } as any}>
        <div className="bt-l">Total PnL · all bots</div>
        <div className="bt-hero-row">
          <CountUp className={`bt-v big ${tone(pnl)}`} value={pnl} fmt={(x) => signed(x)} />
          <CountUp className={`bt-chip ${tone(pnl)}`} value={pnlPct ?? 0} fmt={(x) => (pnlPct == null ? "–" : pct(x, 2, true))} />
        </div>
        <div className="bt-s">
          on {money(bankroll)} paper bankroll · today <span className={tone(today)}>{signed(today)}</span>
          {best && <> · best <b>{best.name}</b> <span className={tone(best.pnl_total)}>{signed(best.pnl_total)}</span></>}
        </div>
      </div>
      <div className="bt-card" style={{ "--i": 1 } as any}>
        <div className="bt-l">Bots</div>
        <div className="bt-v"><CountUp value={running} fmt={int} /><span className="bt-of"> / {bots.length} running</span></div>
        <div className="bt-dots">
          {bots.map((b) => <span key={b.id} className={b.running ? "on" : ""} title={`${b.name}: ${b.running ? "running" : "stopped"}`} />)}
        </div>
      </div>
      <div className="bt-card" style={{ "--i": 2 } as any}>
        <div className="bt-l">Windows traded</div>
        <CountUp className="bt-v" value={sum("windows")} fmt={int} />
        <Meter parts={[{ v: won, c: "var(--up)" }, { v: done - won, c: "var(--surface-3)" }]} />
        <div className="bt-s">{winRate == null ? "none settled yet" : `${pct(winRate, 0)} won · ${int(done)} settled`}</div>
      </div>
      <div className="bt-card" style={{ "--i": 3 } as any}>
        <div className="bt-l">Trades (fills)</div>
        <CountUp className="bt-v" value={fills} fmt={int} />
        <Meter parts={[{ v: maker, c: "var(--s1)" }, { v: taker, c: "var(--s7)" }]} />
        <div className="bt-s"><i className="bt-key" style={{ background: "var(--s1)" }} />{int(maker)} maker · <i className="bt-key" style={{ background: "var(--s7)" }} />{int(taker)} taker</div>
      </div>
      <div className="bt-card" style={{ "--i": 4 } as any}>
        <div className="bt-l">Volume</div>
        <CountUp className="bt-v" value={sum("volume")} fmt={(x) => money(x)} />
        <div className="bt-s">fees paid {money(sum("fees_paid"), 2)}</div>
      </div>
      <div className="bt-card" style={{ "--i": 5 } as any}>
        <div className="bt-l">Rebates (est.)</div>
        <CountUp className="bt-v" value={sum("rebates")} fmt={(x) => money(x, 2)} />
        <div className="bt-s">maker rebates, all bots</div>
      </div>
    </div>
  );
}

// Number that eases from its previous value to the new one (first render counts up from 0).
function CountUp({ value, fmt, className }: { value: number; fmt: (x: number) => string; className?: string }) {
  const [shown, setShown] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    const start = from.current, end = value, t0 = performance.now();
    const dur = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : start === 0 ? 1100 : 600;
    let raf = 0;
    const step = (t: number) => {
      const k = dur ? Math.min(1, (t - t0) / dur) : 1;
      const v = start + (end - start) * (1 - Math.pow(1 - k, 3));
      from.current = v;
      setShown(v);
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value]);
  return <span className={`num ${className || ""}`}>{fmt(shown)}</span>;
}

function Meter({ parts }: { parts: { v: number; c: string }[] }) {
  const total = parts.reduce((a, p) => a + Math.max(0, p.v), 0);
  return (
    <div className="bt-meter">
      {parts.map((p, i) => <span key={i} style={{ width: total ? `${(Math.max(0, p.v) / total) * 100}%` : 0, background: p.c }} />)}
    </div>
  );
}

function NewBot({ onClose, onDone }: { onClose: () => void; onDone: (id: string) => void }) {
  const { data: strategies } = usePoll<any[]>("/api/strategies");
  const [name, setName] = useState("");
  const [strategy, setStrategy] = useState("market_maker");
  const [bankroll, setBankroll] = useState("1000");
  const s = strategies?.find((x) => x.id === strategy);
  async function create() {
    try {
      const b = await api.post("/api/bots", { name: name.trim() || s?.label || "Bot", strategy, config: { bankroll: Number(bankroll) || 1000 } });
      toast(`Created ${b.name}`, "ok");
      onDone(b.id);
    } catch (e: any) { toast(e.message, "err"); }
  }
  return (
    <Modal title="New bot" sub="Starts stopped, on paper. Settings can be changed any time." onClose={onClose}
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" onClick={create}>Create bot</button></>}>
      <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. MM tight edge" autoFocus /></Field>
      <Field label="Strategy" hint={s?.description}>
        <select className="input" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
          {(strategies || []).map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
      </Field>
      <Field label="Paper bankroll (USDC)"><input className="input" value={bankroll} onChange={(e) => setBankroll(e.target.value)} inputMode="decimal" /></Field>
    </Modal>
  );
}

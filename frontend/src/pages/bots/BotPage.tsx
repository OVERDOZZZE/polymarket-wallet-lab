// One bot: Polymarket-style portfolio dashboard (port of the v1 Bot tab), polled from the bot's state file.
import { Fragment, ReactNode, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { C, LineChart } from "../../components/charts";
import { BtcMarket, Card, Empty, Field, LiveBadge, Modal, Pill, Seg, Strip, Table, Tabs, toast } from "../../components/ui";
import { api } from "../../lib/api";
import { ago, cents, clock, dtUTC, hm, money, num, signed, tone } from "../../lib/format";
import { useNow, usePoll } from "../../lib/hooks";
import { BotSettings } from "./BotSettings";

const SECTIONS = ["Live account", "Positions", "Open orders", "Activity", "Signals", "Paper twin", "Live markets", "History", "Diagnostics"] as const;
type Section = (typeof SECTIONS)[number];
// live bots show only real-account data up front; the paper twin (sizing reference) has its own tab
const LIVE_SECTIONS: Section[] = ["Live account", "Signals", "Paper twin", "Live markets", "Diagnostics"];
const PAPER_SECTIONS: Section[] = ["Positions", "Open orders", "Activity", "Signals", "Live markets", "History", "Diagnostics"];
const RANGES: Record<string, number | null> = { "1D": 86400, "1W": 7 * 86400, "1M": 30 * 86400, ALL: null };
const RANGE_SUB: Record<string, string> = { "1D": "Past day", "1W": "Past week", "1M": "Past month", ALL: "All time" };
const left = (end: number, now: number) => (end - now <= 0 ? "ended" : `${clock(end - now)} left`);

export default function BotPage() {
  const { id = "" } = useParams();
  const bot = usePoll<any>(`/api/bots/${id}`, 5000, [id]);
  const st = usePoll<any>(`/api/bots/${id}/state`, 1000, [id]);
  const eq = usePoll<any>(`/api/bots/${id}/equity`, 30000, [id]);
  const [pick, setSec] = useState<Section | "">("");
  const [dlg, setDlg] = useState<"" | "settings" | "reset">("");
  const [busy, setBusy] = useState(false);
  const s = st.data?.state;
  const running = !!st.data?.running;
  const b = bot.data;

  async function act(path: string, msg: string) {
    setBusy(true);
    try { await api.post(`/api/bots/${id}/${path}`); toast(msg, "ok"); } catch (e: any) { toast(e.message, "err"); }
    setBusy(false);
    st.reload(); bot.reload();
  }

  if (bot.error) return <div className="page"><Empty title="Bot not found">{bot.error}</Empty></div>;
  const counts = s ? { Positions: new Set((s.positions || []).map((p: any) => p.slug + p.side)).size,
                       "Open orders": (s.orders || []).filter((o: any) => o.status !== "cancelling").length } : {};
  const tabs = (s?.live ? LIVE_SECTIONS : PAPER_SECTIONS).filter((x) => x !== "Signals" || s?.signals);
  const sec = pick && tabs.includes(pick) ? pick : tabs[0];
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <div className="muted small"><Link to="/bots" style={{ textDecoration: "none" }}>Bots</Link> /</div>
          <h1 className="page-title">{b?.name || id}</h1>
          <p className="page-sub">{b?.strategy_label} · {b?.mode === "paper" ? "Paper trading on live Polymarket data, no real orders" : "LIVE: real orders, sized from a paper twin that trades alongside"}</p>
        </div>
        <span className="spacer" />
        {running
          ? <button className="btn" disabled={busy} onClick={() => act("stop", "Bot stopped")}>Stop</button>
          : <button className="btn primary" disabled={busy || !b} onClick={() => act("start", "Bot started")}>{busy ? "Starting…" : "Start bot"}</button>}
        <button className="btn" disabled={!b} onClick={() => setDlg("settings")}>Settings</button>
        <button className="btn ghost" disabled={!b} onClick={() => setDlg("reset")}>New account</button>
      </div>

      {!s || !s.account ? (
        <Empty title="Bot not started yet">Press Start bot. It gets a paper balance, streams Coinbase, Chainlink and the current BTC windows' order books, and trades them on paper in its own process, so it keeps going while you use other pages or close the browser.</Empty>
      ) : (
        <>
          <div className="grid2">
            {s.live ? <LiveProfileCard s={s} running={running} b={b} /> : <ProfileCard s={s} running={running} b={b} />}
            {s.live ? <LivePnlCard L={s.live} /> : <PnlCard s={s} points={eq.data?.points || []} />}
          </div>
          {s.live ? <LiveStrip s={s} /> : <BotStrip s={s} />}
          <Tabs value={sec} options={tabs} onChange={setSec} counts={s.live ? { "Live account": (s.live.windows || []).filter((w: any) => w.pnl === null).length } : counts} />
          {sec === "Live account" && s.live && <LiveAccount L={s.live} />}
          {sec === "Paper twin" && <PaperTwin s={s} id={id} points={eq.data?.points || []} running={running} />}
          {sec === "Positions" && <Positions s={s} />}
          {sec === "Open orders" && <Orders s={s} />}
          {sec === "Activity" && <Activity s={s} />}
          {sec === "Signals" && <Signals s={s} />}
          {sec === "Live markets" && <Markets s={s} />}
          {sec === "History" && <History id={id} />}
          {sec === "Diagnostics" && <Diagnostics s={s} running={running} id={id} />}
        </>
      )}
      <AboutBot id={id} />
      {dlg === "settings" && b && <BotSettings bot={{ ...b, running }} onClose={() => { setDlg(""); bot.reload(); }} />}
      {dlg === "reset" && b && <ResetDialog bot={b} onClose={() => { setDlg(""); bot.reload(); st.reload(); }} />}
    </div>
  );
}

function ProfileCard({ s, running, b }: { s: any; running: boolean; b: any }) {
  const a = s.account, t = s.totals || {};
  return (
    <div className="card">
      <div className="pm-prof">
        <div className="pm-ava b" />
        <div>
          <div className="pm-name">{s.name || b?.name}<LiveBadge on={running} /></div>
          <div className="pm-meta">Account #{s.account_no} since {new Date((a.since || Date.now() / 1000) * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })} · {a.windows} windows · bankroll {money(a.bankroll)}</div>
        </div>
      </div>
      <div className="pm-stats">
        <div><div className="v">{money(a.portfolio, 2)}</div><div className="l">Portfolio</div></div>
        <div><div className="v">{money(a.available, 2)}</div><div className="l">Cash available</div></div>
        <div><div className="v">{money(a.positions_value, 2)}</div><div className="l">Positions</div></div>
      </div>
      <div className="pm-rows">
        <span className="l">PnL today</span><span className={`v ${tone(a.pnl_today)}`}>{signed(a.pnl_today)}</span>
        <span className="l">Total PnL</span><span className={`v ${tone(a.pnl_total)}`}>{signed(a.pnl_total)} <span className="muted" style={{ fontWeight: 500 }}>({num((a.pnl_total / a.bankroll) * 100, 2, true)}%)</span></span>
        <span className="l">In open orders</span><span className="v">{money(a.reserved, 2)}</span>
        <span className="l">Volume</span><span className="v">{money(t.volume)}</span>
        <span className="l">Biggest win</span><span className="v">{money(a.biggest_win, 2)}</span>
      </div>
    </div>
  );
}

function PnlCard({ s, points }: { s: any; points: [number, number][] }) {
  const [rng, setRng] = useState("1D");
  const a = s.account;
  const now = Date.now() / 1000;
  const { x, y, change } = useMemo(() => {
    const pts = [...points, [now, a.portfolio] as [number, number]];
    const span = RANGES[rng];
    const since = Math.min(a.since || pts[0][0], pts[0][0]);
    const start = span ? Math.max(now - span, since) : since;
    const prior = pts.filter((p) => p[0] <= start);
    const base = prior.length ? prior[prior.length - 1][1] : a.bankroll;
    const sel = [[start, base] as [number, number], ...pts.filter((p) => p[0] > start)];
    return { x: sel.map((p) => p[0]), y: sel.map((p) => p[1] - base), change: a.portfolio - base };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, rng, Math.round(a.portfolio * 100)]);
  return (
    <div className="card">
      <div className="row">
        <span className="t2"><span className={change >= 0 ? "up" : "dn"}>{change >= 0 ? "▲" : "▼"}</span> {s.live ? "Paper twin profit/loss (not real money)" : "Profit/Loss"}</span>
        <span className="spacer" />
        <Seg value={rng} options={Object.keys(RANGES)} onChange={setRng} />
      </div>
      <div className={`big-value ${tone(change)}`}>{signed(change)}</div>
      <div className="muted small" style={{ marginBottom: 8 }}>{RANGE_SUB[rng]}</div>
      <LineChart x={x} ys={[y]} time height={210} zero legend={false}
                 series={[{ label: "P/L", color: C.pnl, width: 2.5, fill: "rgba(139,108,246,0.14)", fmt: (v) => signed(v) }]}
                 yFmt={(v) => "$" + v.toFixed(0)} />
    </div>
  );
}

function BotStrip({ s }: { s: any }) {
  const t = s.totals || {}, vol = t.volume || 0, done = s.windows_done || 0, won = s.windows_won || 0;
  const mo = s.markout_avg, feed = (s.lat?.clob || [null])[0];
  return <Strip items={[
    s.signals && s.strategy === "fast_player"
      ? { label: "Spikes → orders", value: `${(s.counts?.spikes || 0).toLocaleString()} → ${(s.counts?.orders || 0).toLocaleString()}`, sub: `fees paid ${money(t.fees_paid, 2)}` }
      : { label: "Rebates (est.)", value: money(t.rebate_est, 2), sub: vol ? `${((t.rebate_est || 0) / vol * 1e4).toFixed(0)} bps of volume` : "paid daily by Polymarket" },
    { label: "Trading PnL", value: signed(s.trading_pnl || 0), sub: "settled windows, before rebates", tone: tone(s.trading_pnl) },
    { label: "Windows won", value: done ? `${won}/${done}` : "–", sub: done ? `${Math.round(won / done * 100)}% positive` : "none settled yet" },
    { label: "Markout 10s", value: mo === null || mo === undefined ? "–" : `${num(mo, 2, true)}¢`, sub: "negative = getting picked off", tone: tone(mo) },
    { label: "Fills", value: (t.fills || 0).toLocaleString(), sub: `${(t.taker_fills || 0).toLocaleString()} taker` },
    { label: "Book feed", value: feed === null || feed === undefined ? "–" : `${Math.round(feed)} ms`, sub: "Polymarket books, p50", tone: feed > 500 ? "dn" : "" },
  ]} />;
}

// live mode (fast_player): the real account is the main card; the paper twin is shown beside it for comparison
function LiveProfileCard({ s, running, b }: { s: any; running: boolean; b: any }) {
  const L = s.live, a = s.account;
  const [state, cls] = !L.enabled ? ["Not connected", "dn"] : L.halted ? ["Halted: loss limit reached", "dn"] : L.paused ? ["Paused", "warn"] : ["Trading", "up"];
  return (
    <div className="card">
      <div className="pm-prof">
        <div className="pm-ava live" />
        <div>
          <div className="pm-name">{s.name || b?.name}<LiveBadge on={running} /></div>
          <div className="pm-meta"><b className="dn">REAL MONEY</b> · <span className={cls}>{state}</span>{!L.enabled && L.reason ? ` (${L.reason})` : ""} · size = paper × {L.scale}</div>
        </div>
      </div>
      <div className="pm-stats">
        <div><div className="v">{L.usdc == null ? "–" : money(L.usdc, 2)}</div><div className="l">Real wallet USDC</div></div>
        <div><div className={`v ${tone(L.realized)}`}>{signed(L.realized)}</div><div className="l">Live PnL (settled)</div></div>
        <div><div className="v">{money(L.at_risk, 2)}</div><div className="l">At risk now</div></div>
      </div>
      <div className="pm-rows">
        <span className="l">Loss limit left</span><span className={`v ${L.room < 5 ? "dn" : ""}`}>{money(Math.max(0, L.room), 2)} of {money(L.loss_limit, 0)} <span className="muted small">(open $ counts as lost)</span></span>
        <span className="l">Live windows won</span><span className="v">{L.windows_done ? `${L.windows_won}/${L.windows_done}` : "–"}</span>
        <span className="l">Spent live</span><span className="v">{money(L.spent, 2)}</span>
        <span className="l">Live orders filled</span><span className="v">{L.sent ? `${L.filled}/${L.sent}` : "–"}{L.missed ? <span className="muted small"> · {L.missed} missed</span> : null}{L.rejected ? <span className="dn small"> · {L.rejected} rejected</span> : null}</span>
        <span className="l">Order latency</span><span className="v">{L.order_ms ? `${Math.round(L.order_ms)} ms` : "–"} <span className="muted small">p50</span></span>
      </div>
      <p className="hint" style={{ marginTop: 8 }}>Sized from the paper twin (paper $ × {L.scale}, its {money(a.bankroll, 0)} account is in the Paper twin tab). Every number on this page outside that tab is the real wallet.</p>
    </div>
  );
}

// live P/L: cumulative settled PnL of the real account, one point per settled window
function LivePnlCard({ L }: { L: any }) {
  const [rng, setRng] = useState("1D");
  const now = Date.now() / 1000;
  const { x, y, change } = useMemo(() => {
    const pts: [number, number][] = [...(L.pnl_series || []), [now, L.realized || 0]];
    const span = RANGES[rng];
    const start = span ? Math.max(now - span, pts[0][0]) : pts[0][0];
    const prior = pts.filter((p) => p[0] <= start);
    const base = prior.length ? prior[prior.length - 1][1] : 0;
    const sel = [[start, base] as [number, number], ...pts.filter((p) => p[0] > start)];
    return { x: sel.map((p) => p[0]), y: sel.map((p) => p[1] - base), change: (L.realized || 0) - base };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [L.pnl_series, rng, Math.round((L.realized || 0) * 100), Math.floor(now / 60)]);
  return (
    <div className="card">
      <div className="row">
        <span className="t2"><span className={change >= 0 ? "up" : "dn"}>{change >= 0 ? "▲" : "▼"}</span> Live profit/loss <span className="muted small">real wallet, settled windows</span></span>
        <span className="spacer" />
        <Seg value={rng} options={Object.keys(RANGES)} onChange={setRng} />
      </div>
      <div className={`big-value ${tone(change)}`}>{signed(change)}</div>
      <div className="muted small" style={{ marginBottom: 8 }}>{RANGE_SUB[rng]}{L.at_risk > 0 ? ` · ${money(L.at_risk, 2)} in open windows` : ""}</div>
      <LineChart x={x} ys={[y]} time height={210} zero legend={false}
                 series={[{ label: "Live P/L", color: C.pnl, width: 2.5, fill: "rgba(139,108,246,0.14)", fmt: (v) => signed(v) }]}
                 yFmt={(v) => "$" + v.toFixed(0)} />
    </div>
  );
}

function LiveStrip({ s }: { s: any }) {
  const L = s.live, feed = (s.lat?.clob || [null])[0];
  const won = L.windows_won || 0, done = L.windows_done || 0;
  return <Strip items={[
    { label: "Spikes → live orders", value: `${(s.counts?.spikes || 0).toLocaleString()} → ${(L.sent || 0).toLocaleString()}`, sub: L.skipped ? `${L.skipped} not sent (paused / limit / cash)` : "one per paper entry" },
    { label: "Fill rate", value: L.sent ? `${Math.round(L.filled / L.sent * 100)}%` : "–", sub: L.sent ? `${L.filled} filled · ${L.missed} missed · ${L.rejected} rejected` : "no live orders yet", tone: L.sent && L.filled < L.sent / 2 ? "warn" : "" },
    { label: "Live PnL", value: signed(L.realized || 0), sub: "settled windows, after fees", tone: tone(L.realized) },
    { label: "Live windows won", value: done ? `${won}/${done}` : "–", sub: done ? `${Math.round(won / done * 100)}% positive` : "none settled yet" },
    { label: "Filled $", value: money(L.spent_filled || 0, 2), sub: L.avg_price ? `avg price ${cents(L.avg_price)}` : "nothing bought yet" },
    { label: "Book feed", value: feed === null || feed === undefined ? "–" : `${Math.round(feed)} ms`, sub: "Polymarket books, p50", tone: feed > 500 ? "dn" : "" },
  ]} />;
}

// the paper twin of a live bot: same signals, simulated fills; its entries size the live orders
function PaperTwin({ s, id, points, running }: { s: any; id: string; points: [number, number][]; running: boolean }) {
  return (
    <div className="col">
      <p className="hint">Paper only, no real money. The twin trades every signal on simulated fills (order latency {s.cfg?.latency_ms ?? "–"} ms); each of its entries sends one real order of its $ × {s.live.scale}. Compare its fills with the Live account tab to see how far paper is from reality.</p>
      <div className="grid2">
        <ProfileCard s={s} running={running} b={null} />
        <PnlCard s={s} points={points} />
      </div>
      <BotStrip s={s} />
      <div className="t2">Open positions</div>
      <Positions s={s} />
      <div className="t2">Activity</div>
      <Activity s={s} />
      <div className="t2">Settled windows</div>
      <History id={id} />
    </div>
  );
}

const LIVE_RESULT: Record<string, [string, string]> = {
  filled: ["Bought", "up"], partial: ["Bought (part)", "up"], missed: ["Missed: no fill at limit", "warn"], error: ["Rejected", "dn"],
  sending: ["Sending", ""], paused: ["Paused", "muted"], loss_limit: ["Loss limit", "dn"], no_cash: ["No cash", "warn"],
  no_gateway: ["Not connected", "dn"], scalp_not_live: ["Scalp not live", "muted"],
};

function LiveAccount({ L }: { L: any }) {
  const now = Date.now() / 1000;
  const log = [...(L.log || [])].reverse();
  return (
    <div className="col">
      {(L.gw_errors || []).length > 0 && <p className="hint dn">{L.gw_errors.map((e: any) => `${hm(e[0])} ${e[1]}`).join(" · ")}</p>}
      <div className="card flush">
        <div className="t2" style={{ padding: "10px 14px" }}>Live orders</div>
        {!log.length ? <Empty title="No live orders yet">Each paper entry also sends a real order of paper $ × {L.scale}.</Empty> : (
          <Table rows={log} cols={[
            { key: "ts", label: "When", align: "l", render: (r: any) => <span className="muted">{ago(r.ts, now)}</span> },
            { key: "m", label: "Market", align: "l", render: (r: any) => { const st = Number(r.slug.split("-").pop()); return mktCell(r.win, st, st + r.win * 60); } },
            { key: "side", label: "Side", render: (r: any) => <Pill side={r.side}>{cents(r.limit)}</Pill> },
            { key: "usd", label: "Paper → live $", render: (r: any) => <>{money(r.paper_usd, 2)} <span className="muted">→</span> {money(r.usd, 2)}</> },
            { key: "fill", label: "Filled", render: (r: any) => r.spent ? <>{money(r.spent, 2)} <span className="muted small">{num(r.shares, 1)} @ {cents(r.avg)}</span></> : "–" },
            { key: "ms", label: "Latency", title: "Signal to order response: signing + Polymarket's reply", render: (r: any) => r.ms ? <span title={r.sign_ms != null ? `sign ${num(r.sign_ms, 1)} ms · post ${Math.round(r.post_ms)} ms` : ""}>{Math.round(r.ms)} ms{r.sign_ms != null && <span className="muted small"> ({Math.round(r.sign_ms)} + {Math.round(r.post_ms)})</span>}</span> : "–" },
            { key: "res", label: "Result", align: "l", render: (r: any) => { const [t, c] = LIVE_RESULT[r.result] || [r.result, ""]; return <span className={c} title={r.error || ""}>{t}</span>; } },
          ]} />
        )}
      </div>
      <div className="card flush">
        <div className="t2" style={{ padding: "10px 14px" }}>Live windows</div>
        {!(L.windows || []).length ? <Empty title="No live windows yet" /> : (
          <Table rows={L.windows} cols={[
            { key: "m", label: "Market", align: "l", render: (w: any) => mktCell(w.win, w.start, w.end) },
            { key: "pos", label: "Held", render: (w: any) => <>{w.up_sh > 0 && <Pill side="up">{num(w.up_sh, 1)}</Pill>} {w.down_sh > 0 && <Pill side="down">{num(w.down_sh, 1)}</Pill>}</> },
            { key: "cost", label: "Cost", render: (w: any) => money(w.up_cost + w.down_cost, 2) },
            { key: "win", label: "Winner", render: (w: any) => w.winner ? <Pill side={w.winner} /> : <span className="muted">{w.end > now ? "open" : "awaiting result"}</span> },
            { key: "pnl", label: "PnL", render: (w: any) => w.pnl === null ? "–" : <span className={tone(w.pnl)}>{signed(w.pnl)}</span> },
          ]} />
        )}
        <p className="hint" style={{ padding: "8px 14px" }}>Shares are after the taker fee (taken in shares, as in paper). Winning shares may need redeeming on Polymarket before the USDC is back in the wallet.</p>
      </div>
    </div>
  );
}

const mktCell = (win: number, start: number, end: number, sub = "") => <BtcMarket title={`Bitcoin Up or Down · ${win}m`} sub={`${hm(start)}–${hm(end)}${sub}`} />;

function Positions({ s }: { s: any }) {
  const now = useNow(1000);
  const ps = [...(s.positions || [])].sort((a: any, b: any) => a.end - b.end || a.win - b.win || a.side.localeCompare(b.side));
  if (!ps.length) return <Empty title="No open positions">They appear as soon as a bid fills, and pay out when the window settles.</Empty>;
  let tv = 0, tc = 0;
  ps.forEach((p: any) => { tv += p.value ?? p.cost; tc += p.cost; });
  return (
    <div className="card flush">
      <Table rows={ps} cols={[
        { key: "m", label: "Market", align: "l", render: (p: any) => mktCell(p.win, p.start, p.end, p.state === "resolving" ? " · settling" : ` · ${left(p.end, now)}`) },
        { key: "side", label: "Outcome", render: (p: any) => <Pill side={p.side} /> },
        { key: "avg", label: "Avg → now", render: (p: any) => <>{cents(p.avg)} <span className="muted">→</span> {cents(p.price)}</> },
        { key: "shares", label: "Shares", render: (p: any) => num(p.shares, 1) },
        { key: "cost", label: "Cost", render: (p: any) => money(p.cost, 2) },
        { key: "value", label: "Value", render: (p: any) => money(p.value, 2) },
        { key: "pl", label: "P/L", sort: (p: any) => (p.value ?? p.cost) - p.cost, render: (p: any) => p.value === null ? "–" : <span className={tone(p.value - p.cost)}>{signed(p.value - p.cost)}</span> },
      ]} footer={<tfoot><tr><td>Total</td><td /><td /><td /><td>{money(tc, 2)}</td><td>{money(tv, 2)}</td><td className={tone(tv - tc)}>{signed(tv - tc)}</td></tr></tfoot>} />
      <p className="hint" style={{ padding: "8px 14px" }}>Value uses the order book mid. Each winning share pays $1 when the window settles.{s.cfg?.exit_mode === "scalp" ? " This bot sells its shares back into the bid once the book has repriced." : " The bot never sells."}</p>
    </div>
  );
}

function Orders({ s }: { s: any }) {
  const now = useNow(1000);
  const os = s.orders || [];
  if (!os.length) return <Empty title="No open bids right now">The bot bids from a few seconds after a window opens until just before it closes, and pulls its bids when BTC moves fast.</Empty>;
  const dot = (c: string) => <span className="dot" style={{ color: c, display: "inline-block", marginRight: 6 }} />;
  return (
    <div className="card flush">
      <Table rows={os} cols={[
        { key: "m", label: "Market", align: "l", render: (o: any) => mktCell(o.win, o.start, o.end, ` · ${left(o.end, now)}`) },
        { key: "side", label: "Side", render: (o: any) => <><Pill side={o.side} /><span className="muted">Buy</span></> },
        { key: "price", label: "Price", render: (o: any) => cents(o.price) },
        { key: "filled", label: "Filled", render: (o: any) => `${num(o.size - o.remaining, 1)} / ${num(o.size, 0)}` },
        { key: "total", label: "Total", render: (o: any) => money(o.remaining * o.price, 2) },
        { key: "queue", label: "Queue ahead", title: "Shares at the same price that were there first", render: (o: any) => num(o.queue, 0) },
        { key: "status", label: "Status", render: (o: any) => <>{dot(o.status === "live" ? "var(--up)" : o.status === "sending" ? "var(--warn)" : "var(--muted)")}{o.status} <span className="muted small">{o.age.toFixed(0)}s</span></> },
      ]} />
    </div>
  );
}

function Activity({ s }: { s: any }) {
  const now = Date.now() / 1000;
  const ev = [
    ...(s.fills || []).map((f: any) => ({ ts: f.ts, kind: "fill", d: f })),
    ...(s.settled || []).map((r: any) => ({ ts: r.settled_ts || r.start + r.win * 60 + 2, kind: "settle", d: r })),
  ].sort((a, b) => b.ts - a.ts).slice(0, 100);
  if (!ev.length) return <Empty title="Nothing yet" />;
  return (
    <div className="card flush">
      <Table rows={ev} cols={[
        { key: "kind", label: "Type", align: "l", render: (e: any) => e.kind === "fill" ? <><b>{e.d.shares < 0 ? "Sell" : "Buy"}</b> <span className="muted small">{e.d.maker ? "maker" : "taker"}</span></> : <><b>{e.d.winner ? "Settled" : "Settling"}</b> <span className="muted small">{e.d.fills} fills</span></> },
        { key: "m", label: "Market", align: "l", render: (e: any) => { const st = e.kind === "fill" ? Number(e.d.slug.split("-").pop()) : e.d.start; return mktCell(e.d.win, st, st + e.d.win * 60); } },
        { key: "o", label: "Outcome", render: (e: any) => e.kind === "fill" ? <Pill side={e.d.side}>{cents(e.d.price)}</Pill> : e.d.winner ? <><Pill side={e.d.winner} /><span className="muted">won</span></> : <span className="muted">awaiting result</span> },
        { key: "det", label: "Details", render: (e: any) => e.kind === "fill" ? `${num(Math.abs(e.d.shares), 1)} shares` : e.d.net_pnl === null || e.d.net_pnl === undefined ? "" : <>net <span className={tone(e.d.net_pnl)}>{signed(e.d.net_pnl)}</span></> },
        { key: "amt", label: "Amount", render: (e: any) => e.kind === "fill" ? (e.d.usd < 0 ? <span className="up">+{money(-e.d.usd, 2)}</span> : <span className="dn">−{money(e.d.usd, 2)}</span>) : e.d.credited ? <span className="up">+{money(e.d.credited, 2)}</span> : "–" },
        { key: "ts", label: "When", render: (e: any) => <span className="muted">{ago(e.ts, now)}</span> },
      ]} />
    </div>
  );
}

// fast_player: every spike seen per window, what the bot decided and why, and how its orders filled.
// guard_maker: the same spikes, with the side whose bids were pulled.
const RESULT: Record<string, [string, string]> = {
  filled: ["Bought", "up"], partial: ["Bought (part)", "up"], missed: ["Missed: book moved", "warn"], no_book: ["Failed: no book", "dn"],
  order: ["Sending", ""], exit: ["Sold", "up"], exit_partial: ["Sold (part)", "up"], exit_missed: ["Sell missed", "warn"],
  exit_no_book: ["Sell failed", "dn"], no_edge: ["No edge: already repriced", "muted"], price_cap: ["Above max price", "muted"],
  price_floor: ["Below min price", "muted"], max_entries: ["Window full", "muted"], cooldown: ["Cooldown", "muted"],
  busy: ["Order in flight", "muted"], no_model: ["No price to beat yet", "muted"], stale_book: ["Book stale", "dn"],
  stale_chainlink: ["Chainlink stale", "dn"], risk_limit: ["Risk limit", "warn"], cash: ["No cash", "warn"],
  pulled: ["Pulled bids", "up"], none_resting: ["Nothing resting", "muted"],
  daily_loss: ["Daily loss halt", "dn"], strike_mismatch: ["Price to beat mismatch", "dn"], no_ask: ["No ask", "muted"],
};

function Signals({ s }: { s: any }) {
  const now = Date.now() / 1000;
  const [all, setAll] = useState(false);
  const rows = [...(s.signals || [])].reverse().filter((r: any) => all || !["max_entries", "cooldown", "busy"].includes(r.result));
  const sp = s.spike, ex = s.exchanges || {};
  return (
    <div className="col">
      <div className="row wrap">
        <span className="chip"><span className="dot" style={{ color: sp ? (sp.move > 0 ? "var(--up)" : "var(--down)") : "var(--muted)" }} />{sp ? `Spike ${num(sp.move, 2, true)} bps` : "No spike"}</span>
        {Object.entries(ex).map(([k, v]: any) => v[0] !== null && <span key={k} className="chip">{k} {num(v[0], 2)} <span className="muted small">{v[1] !== null ? `${(v[1] / 1000).toFixed(1)} s ago` : ""}</span></span>)}
        <span className="spacer" />
        <label className="row small muted" style={{ gap: 6 }}><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> show cooldown / window-full rows</label>
      </div>
      {!rows.length ? <Empty title="No spikes yet">Each BTC move of the set size within the lookback shows here, per window, with what the bot did and why.</Empty> : (
        <div className="card flush">
          <Table rows={rows} cols={[
            { key: "ts", label: "When", align: "l", render: (r: any) => <span className="muted">{ago(r.ts, now)}</span> },
            { key: "m", label: "Market", align: "l", render: (r: any) => { const st = Number(r.slug.split("-").pop()); return mktCell(r.win, st, st + r.win * 60, ` · ${Math.round(r.t_left)}s left`); } },
            { key: "side", label: "Side", render: (r: any) => <Pill side={r.side} /> },
            { key: "move", label: "Move", render: (r: any) => r.move === null ? "–" : `${num(r.move, 2, true)} bps` },
            { key: "ask", label: "Ask → fair", render: (r: any) => r.ask === null ? "–" : <>{cents(r.ask)} <span className="muted">→</span> {cents(r.fair)}</> },
            { key: "edge", label: "Edge", title: "Entries: fair − cost per share after fees. Exits: gain per share after fees.", render: (r: any) => r.edge === null ? "–" : <span className={tone(r.edge)}>{num(r.edge * 100, 1, true)}¢</span> },
            { key: "limit", label: "Limit", render: (r: any) => r.limit === null ? "–" : cents(r.limit) },
            { key: "res", label: s.live ? "Paper twin" : "Result", align: "l", render: (r: any) => { const [t, c] = RESULT[r.result] || [r.result, ""]; return <span className={c}>{t}{r.result === "pulled" ? <span className="muted small"> {r.filled} order{r.filled === 1 ? "" : "s"}</span> : r.filled ? <span className="muted small"> {num(r.filled, 1)} @ {cents(r.avg)}</span> : null}</span>; } },
            ...(s.live ? [{ key: "live", label: "Live order", align: "l" as const, render: (r: any) => { if (!r.live) return <span className="muted">–</span>; const [t, c] = LIVE_RESULT[r.live] || [r.live, ""]; return <span className={c}>{t}</span>; } }] : []),
          ]} />
        </div>
      )}
    </div>
  );
}

function Markets({ s }: { s: any }) {
  const now = useNow(500);
  const ms = (s.markets || []).filter((m: any) => m.state !== "settled" || now < m.end + 20);
  const cb = s.cb_series || [], cl = s.cl_series || [];
  const basis = s.basis || 0;
  const { x, ys } = useMemo(() => {
    const xs = Array.from(new Set([...cb.map((p: any) => p[0]), ...cl.map((p: any) => p[0])])).sort((a: any, b: any) => a - b) as number[];
    const cbm = new Map(cb.map((p: any) => [p[0], p[1] + basis])), clm = new Map(cl.map((p: any) => [p[0], p[1]]));
    return { x: xs, ys: [xs.map((t) => (cbm.get(t) as number) ?? null), xs.map((t) => (clm.get(t) as number) ?? null)] };
  }, [cb, cl, basis]);
  const strikes = (s.markets || []).filter((m: any) => m.strike && m.start <= now && now < m.end).map((m: any) => ({ y: m.strike, color: "rgba(236,235,232,.5)", label: `${m.win}m price to beat` }));
  return (
    <>
      {!ms.length ? <Empty title="No windows tracked yet" /> : (
        <div className="grid3">{ms.slice(0, 6).map((m: any) => <MarketCard key={m.slug} m={m} s={s} now={now} />)}</div>
      )}
      {x.length > 0 && (
        <Card title="BTC price, last 5 minutes" style={{ marginTop: 14 }}>
          <LineChart x={x} ys={ys} time height={260} hlines={strikes} yFmt={(v) => "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 })}
                     xFmt={(v) => new Date(v * 1000).toLocaleTimeString([], { hour12: false })}
                     series={[{ label: "Coinbase (leads)", color: C.s2, fmt: (v) => money(v, 2) }, { label: "Chainlink (settles)", color: C.s1, fmt: (v) => money(v, 2) }]} />
        </Card>
      )}
    </>
  );
}

function MarketCard({ m, s, now }: { m: any; s: any; now: number }) {
  const state = ({ pending: "waiting", quoting: "quoting", closed: "settling", settled: "settled" } as any)[m.state];
  const bu = m.book.up, bd = m.book.down;
  const midUp = bu.bid !== null && bu.ask !== null ? (bu.bid + bu.ask) / 2 : null;
  const dist = s.spot !== null && m.strike !== null ? s.spot - m.strike : null;
  const q = (qs: any[]) => qs.map(([p, sz]) => `${Math.round(p * 100)}¢×${sz}`).join(" ") || "–";
  const clk = now < m.start ? `opens in ${Math.round(m.start - now)}s` : now < m.end ? clock(m.end - now) : "closed";
  const rows: [string, ReactNode][] = [
    ["Status", `${state} · book ${m.ws}`],
    ["Price to beat", m.strike === null ? "–" : money(m.strike, 2)],
    ["BTC vs price to beat", dist === null ? "–" : <span className={tone(dist)}>{num(dist, 2, true)}</span>],
    ["Up: book / our fair", `${cents(midUp)} / ${cents(m.fair_blend)}`],
    ["Up bid · ask", `${cents(bu.bid)} · ${cents(bu.ask)}`],
    ["Down bid · ask", `${cents(bd.bid)} · ${cents(bd.ask)}`],
    ["Our Up bids", q(m.quotes.up)], ["Our Down bids", q(m.quotes.down)],
    ["Holding", `${num(m.shares.up, 0)} Up · ${num(m.shares.down, 0)} Down (${money(m.cost.up + m.cost.down, 2)})`],
    ["If Up / if Down", <><span className={tone(m.pnl_up)}>{signed(m.pnl_up)}</span> / <span className={tone(m.pnl_down)}>{signed(m.pnl_down)}</span></>],
  ];
  return (
    <div className="mk-card">
      <div className="mk-head"><b>BTC {m.win}m · {hm(m.start)}–{hm(m.end)}</b><span className="num" style={{ fontWeight: 650, fontSize: "1.1rem" }}>{clk}</span></div>
      {m.fair_blend !== null && <div className="prob" title="Green = our fair P(up); tick = book mid"><span style={{ width: `${m.fair_blend * 100}%` }} />{midUp !== null && <i style={{ left: `${midUp * 100}%` }} />}</div>}
      <div className="mk-grid">{rows.map(([k, v]) => <Fragment key={k}><div className="k">{k}</div><div className="v">{v}</div></Fragment>)}</div>
    </div>
  );
}

function History({ id }: { id: string }) {
  const { data } = usePoll<any>(`/api/bots/${id}/history`, 15000, [id]);
  const w = data?.windows || [];
  if (data && !w.length) return <Empty title="No settled windows yet">A window settles about 2 seconds after it closes.</Empty>;
  return (
    <div className="card flush">
      <Table rows={w} maxHeight={560} cols={[
        { key: "start", label: "Window (UTC)", align: "l", render: (r: any) => `${r.win}m ${dtUTC(r.start)}` },
        { key: "winner", label: "Result", render: (r: any) => r.winner ? <><Pill side={r.winner} />{r.official ? "" : <span className="muted small">est.</span>}</> : "?" },
        { key: "fills", label: "Fills" },
        { key: "up_sh", label: "Up sh", render: (r: any) => num(r.up_sh, 0) },
        { key: "down_sh", label: "Down sh", render: (r: any) => num(r.down_sh, 0) },
        { key: "cost", label: "Cost", render: (r: any) => money(r.cost, 2) },
        { key: "pair_cost", label: "Pair cost", title: "Avg Up + avg Down price; under 1 locks profit on the matched part", render: (r: any) => num(r.pair_cost, 3) },
        { key: "trading_pnl", label: "Trading", render: (r: any) => <span className={tone(r.trading_pnl)}>{signed(r.trading_pnl)}</span> },
        { key: "rebate_est", label: "Rebate", render: (r: any) => money(r.rebate_est, 3) },
        { key: "net_pnl", label: "Net", render: (r: any) => <span className={tone(r.net_pnl)}>{signed(r.net_pnl)}</span> },
      ]} />
    </div>
  );
}

function Diagnostics({ s, running, id }: { s: any; running: boolean; id: string }) {
  const lat = s.lat || {}, stt = s.status || {};
  const [rep, setRep] = useState<any>(null);
  const feeds: [string, string, number][] = [["Coinbase", "coinbase", 250], ["Kraken", "kraken", 250], ["Binance", "binance", 400], ["Binance futures", "binance_fut", 400],
    ["Chainlink", "chainlink", 3000], ["Books", "clob", 500]].filter(([, k]) => k in stt && (stt[k] !== "stopped" || !running)) as any;
  return (
    <div className="col">
      <div className="row wrap">
        {feeds.map(([name, key, warn]) => {
          const p50 = (lat[key] || [null])[0];
          const ok = stt[key] === "connected" || String(stt[key]).includes("live");
          return <span key={key} className="chip"><span className="dot" style={{ color: !ok ? "var(--down)" : p50 > warn ? "var(--warn)" : "var(--up)" }} />{name}: {stt[key]}{p50 !== null && p50 !== undefined ? ` · ${Math.round(p50)} ms` : ""}</span>;
        })}
        <span className="chip">Engine {running ? `running · pid ${s.pid}` : "stopped"}{lat.decide_us ? ` · decision ${Math.round(lat.decide_us[0])} µs` : ""}</span>
        <span className="spacer" />
        <button className="btn sm" onClick={() => api.get(`/api/bots/${id}/latency`).then(setRep).catch((e) => toast(e.message, "err"))}>Latency report</button>
      </div>
      <div className="grid2">
        <Card title="Latency (exchange → received; decision in µs)">
          <Table rows={Object.entries(lat).filter(([, v]) => v).map(([k, v]: any) => ({ k, p50: v[0], p90: v[1] }))} cols={[
            { key: "k", label: "Stage", align: "l" }, { key: "p50", label: "p50", render: (r: any) => num(r.p50, 1) }, { key: "p90", label: "p90", render: (r: any) => num(r.p90, 1) }]} />
          <p className="hint" style={{ marginTop: 8 }}>Clock offset (NTP): {num(s.clock_offset_ms, 1)} ms</p>
        </Card>
        <Card title="Model inputs">
          <div className="pm-rows">
            <span className="l">Coinbase mid</span><span className="v">{num(s.coinbase?.mid, 2)}</span>
            <span className="l">Chainlink</span><span className="v">{num(s.chainlink?.[1], 2)}</span>
            <span className="l">Basis (Chainlink − Coinbase)</span><span className="v">{num(s.basis, 2)}</span>
            <span className="l">Volatility</span><span className="v">{num(s.vol_bps, 3)} bps/√s</span>
            <span className="l">Uptime</span><span className="v">{Math.round((s.uptime_s || 0) / 60)} min</span>
          </div>
          <pre className="log" style={{ marginTop: 10 }}>{JSON.stringify(s.counts, null, 1)}</pre>
        </Card>
      </div>
      {(s.errors || []).length > 0 && (
        <Card title="Recent errors / reconnects">
          {[...s.errors].reverse().map(([t, e]: any, i: number) => <div key={i} className="small"><span className="muted">{new Date(t * 1000).toLocaleTimeString([], { hour12: false })}</span> {e}</div>)}
        </Card>
      )}
      {rep && (
        <Modal title="Latency report" sub={`${rep.measured_at} · ${(rep.uptime_s / 60).toFixed(1)} min run`} onClose={() => setRep(null)} wide>
          <Table rows={Object.values(rep.stages)} cols={[
            { key: "what", label: "Stage", align: "l" }, { key: "unit", label: "Unit" }, { key: "n", label: "n" },
            { key: "p50", label: "p50", render: (r: any) => num(r.p50, 1) }, { key: "p90", label: "p90", render: (r: any) => num(r.p90, 1) },
            { key: "p99", label: "p99", render: (r: any) => num(r.p99, 1) }, { key: "max", label: "max", render: (r: any) => num(r.max, 1) }]} />
          <p className="hint">Order send: {rep.order_send_ms.value} ms ({rep.order_send_ms.source}).</p>
        </Modal>
      )}
    </div>
  );
}

const KIND: Record<string, string> = { created: "Created", rebuilt: "Rebuilt", audit: "Audit", simulator: "Simulator",
                                       settings: "Settings", account: "Account", note: "Note" };

// What the bot does and everything that happened to it (created, rebuilt, audited, settings, accounts), newest first.
function AboutBot({ id }: { id: string }) {
  const d = usePoll<any>(`/api/bots/${id}/changes`, 30000, [id]);
  const [text, setText] = useState("");
  async function add() {
    try { await api.post(`/api/bots/${id}/changes`, { text }); setText(""); d.reload(); } catch (e: any) { toast(e.message, "err"); }
  }
  const s = d.data?.strategy;
  return (
    <Card title="About this bot" className="about">
      {s && <p className="hint"><b style={{ color: "var(--text)" }}>{s.label}.</b> {s.description}</p>}
      <div className="history">
        {(d.data?.changes || []).map((c: any) => (
          <div key={c.id} className="history-row">
            <span className="muted num">{dtUTC(c.ts)} UTC</span>
            <span><span className="chip">{KIND[c.kind] || c.kind}</span></span>
            <span>{c.text}</span>
          </div>
        ))}
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <input className="input" placeholder="Add a note to the history…" value={text} onChange={(e) => setText(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) add(); }} />
        <button className="btn" disabled={!text.trim()} onClick={add}>Add note</button>
      </div>
    </Card>
  );
}

function ResetDialog({ bot, onClose }: { bot: any; onClose: () => void }) {
  const [amount, setAmount] = useState(String(bot.config.bankroll || 1000));
  async function go() {
    try { await api.post(`/api/bots/${bot.id}/reset`, { bankroll: Number(amount) }); toast("New paper account started", "ok"); onClose(); }
    catch (e: any) { toast(e.message, "err"); }
  }
  return (
    <Modal title="Start a new paper account" onClose={onClose}
           sub="The current results stay in the database under the old account number. Settings are kept."
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" onClick={go}>Start new account</button></>}>
      <Field label="Starting balance (USDC)"><input className="input" value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" /></Field>
    </Modal>
  );
}

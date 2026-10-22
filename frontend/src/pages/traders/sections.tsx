// Analysis sections of a trader (all read from data/traders/<id>/analysis via the API).
import { useEffect, useMemo, useState } from "react";
import { Bars, C, histogram, LineChart } from "../../components/charts";
import { Card, Empty, Kpis, Pill, Section, Seg, Table, toast } from "../../components/ui";
import { api } from "../../lib/api";
import { cents, dtUTC, money, num, pct, signed, tone } from "../../lib/format";
import { usePoll } from "../../lib/hooks";

const signedColor = (v: number) => (v >= 0 ? C.s1 : C.down);

// ------------------------------------------------------------------ overview
export function Overview({ t, S }: { t: any; S: any }) {
  const { data: pnl } = usePoll<any>(`/api/traders/${t.id}/pnl`, 0, [S.analyzed_at]);
  const { data: tables } = usePoll<any>(`/api/traders/${t.id}/tables`, 0, [S.analyzed_at]);
  const daily = tables?.daily || [];
  const hist = useMemo(() => histogram(pnl?.hist || [], 50), [pnl]);
  return (
    <>
      <Kpis items={[
        { label: "Total PnL", value: signed(S.total_pnl, 0), tone: tone(S.total_pnl), sub: `${(S.resolved || 0).toLocaleString()} resolved windows`, help: "Cash flows plus final resolution per window. Fees and rebates are not included." },
        { label: "Capital deployed", value: money(S.deployed), sub: "sum of buy cost" },
        { label: "Return per $", value: pct(S.roi, 2, true), tone: tone(S.roi), sub: "PnL / deployed" },
        { label: "Windows won", value: pct(S.win_rate_windows), sub: "share with PnL > 0" },
        { label: "Sharpe", value: num(S.sharpe, 1), sub: "daily, annualised", help: "Needs more than 3 days of data." },
        { label: "Max drawdown", value: money(S.max_drawdown), sub: "peak to trough" },
        { label: "Avg day", value: signed(S.daily_mean, 0), tone: tone(S.daily_mean), sub: `${pct(S.days_positive, 0)} of days positive` },
        { label: "Windows traded", value: (S.windows || 0).toLocaleString(), sub: `${pct(S.participation, 0)} of all windows` },
        { label: "Two-sided", value: pct(S.two_sided_share, 0), sub: "bought both Up and Down" },
        { label: "Median pair cost", value: num(S.pair_cost_median, 3), sub: "below 1.000 locks profit", help: "avg Up price + avg Down price in two-sided windows" },
        { label: "Top 1% windows", value: pct(S.top1pct_share, 0), sub: "share of total PnL" },
        { label: "Data check", value: num(S.sanity_ratio, 3), sub: "about 1.000 = history complete", help: "Reconstructed payout divided by actual redeemed cash." },
      ]} />
      <Card title="Cumulative PnL">
        {pnl?.t?.length ? <LineChart x={pnl.t} ys={[pnl.cum]} time height={320} zero legend={false}
          series={[{ label: "Cumulative PnL", color: C.s1, fill: "rgba(57,135,229,0.10)", fmt: (v) => money(v) }]} yFmt={(v) => "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 })} /> : <Empty title="No resolved windows yet" />}
      </Card>
      <div className="grid2" style={{ marginTop: 14 }}>
        <Card title="Daily PnL">
          <Bars labels={daily.map((d: any) => String(d.date).slice(5))} values={daily.map((d: any) => d.pnl)} color={signedColor} fmt={(v) => "$" + Math.round(v).toLocaleString()}
                tip={(i) => <><div className="k">{daily[i].date}</div><b>{signed(daily[i].pnl, 0)}</b><div className="k">deployed {money(daily[i].deployed)} · {daily[i].windows} windows</div></>} />
        </Card>
        <Card title="PnL per window (1st to 99th percentile)">
          <Bars labels={hist.labels} values={hist.counts} color={(_, i) => (hist.edges[i] >= 0 ? C.s1 : C.down)} fmt={(v) => String(Math.round(v))}
                tip={(i) => <><div className="k">${hist.edges[i].toFixed(1)} …</div><b>{hist.counts[i]} windows</b></>} />
        </Card>
      </div>
    </>
  );
}

// ------------------------------------------------------------------ timing & price
export function TimingPrice({ t, S }: { t: any; S: any }) {
  const { data } = usePoll<any>(`/api/traders/${t.id}/tables`, 0, [S.analyzed_at]);
  if (!data) return <p className="hint">Loading…</p>;
  const tb = data.timing || [], pb = data.price || [], hr = data.hourly || [];
  const bucketCols = (first: string) => [
    { key: first, label: "Bucket", align: "l" as const },
    { key: "trades", label: "Buys", render: (r: any) => (r.trades ?? 0).toLocaleString() },
    { key: "usd", label: "Volume", render: (r: any) => money(r.usd) },
    { key: "pnl", label: "PnL", render: (r: any) => <span className={tone(r.pnl)}>{signed(r.pnl, 0)}</span> },
    { key: "roi", label: "ROI", render: (r: any) => pct(r.roi, 2, true) },
    { key: "avg_px", label: "Avg price", render: (r: any) => num(r.avg_px, 3) },
    { key: "win_rate", label: "Win rate", render: (r: any) => num(r.win_rate, 3) },
    { key: "edge", label: "Edge", render: (r: any) => num(r.edge, 3, true) },
  ];
  return (
    <>
      <div className="grid2">
        <Card title="ROI by seconds into the window">
          <Bars labels={tb.map((r: any) => r.bin)} values={tb.map((r: any) => (r.roi || 0) * 100)} color={signedColor} fmt={(v) => v.toFixed(1) + "%"}
                tip={(i) => <><div className="k">{tb[i].bin}</div><b>ROI {pct(tb[i].roi, 2)}</b><div className="k">{(tb[i].trades || 0).toLocaleString()} buys · PnL {signed(tb[i].pnl, 0)}</div></>} />
          {S.first_buy_median !== null && S.first_buy_median !== undefined && <p className="hint" style={{ marginTop: 8 }}>First buy in a window: median {S.first_buy_median.toFixed(0)}s, 10th pct {S.first_buy_p10.toFixed(0)}s, 90th pct {S.first_buy_p90.toFixed(0)}s. A tight spread suggests a scheduled bot.</p>}
          <Table rows={tb} cols={bucketCols("bin")} />
        </Card>
        <Card title="Entry price vs win rate">
          <LineChart x={pb.map((r: any) => r.avg_px)} ys={[pb.map((r: any) => r.avg_px), pb.map((r: any) => r.win_rate)]} height={300} xRange={[0, 1]} yRange={[0, 1]}
                     xFmt={(v) => v.toFixed(2)} yFmt={(v) => v.toFixed(1)}
                     series={[{ label: "Fair (price = win rate)", color: C.muted, width: 1, dash: [5, 5], fmt: (v) => v.toFixed(3) },
                              { label: "Actual win rate", color: C.s1, points: true, fmt: (v) => v.toFixed(3) }]} />
          <p className="hint" style={{ marginTop: 8 }}>Points above the dashed line: buys at that price won more often than the price implied.</p>
          <Table rows={pb} cols={bucketCols("bin")} />
        </Card>
      </div>
      <Section title="Hour of day (UTC)" />
      <div className="grid2">
        <Card title="Trades"><Bars labels={hr.map((r: any) => String(r.hour))} values={hr.map((r: any) => r.trades)} color={() => C.s1} fmt={(v) => Math.round(v).toLocaleString()} /></Card>
        {hr.length > 0 && "pnl" in hr[0] && <Card title="Buy payoff"><Bars labels={hr.map((r: any) => String(r.hour))} values={hr.map((r: any) => r.pnl)} color={signedColor} fmt={(v) => "$" + Math.round(v).toLocaleString()} /></Card>}
      </div>
    </>
  );
}

// ------------------------------------------------------------------ signals
export function Signals({ S, t }: { S: any; t: any }) {
  const { data } = usePoll<any>(`/api/traders/${t.id}/tables`, 0, [S.analyzed_at]);
  const sc = S.signals || {};
  const de = data?.edge_deciles || [];
  return (
    <>
      {Object.keys(sc).length ? (
        <>
          <Section title="Signal tests against the reference price" />
          <Kpis items={[
            { label: "Buys the favourite", value: pct(sc.aligned, 0), sub: "side currently winning" },
            { label: "Follows 5s momentum", value: pct(sc.mom, 0), sub: `after >2bps moves, n=${(sc.mom_n || 0).toLocaleString()}`, help: "About 50% means no latency play." },
            { label: "Mean model edge", value: num(sc.mean_edge, 3, true), sub: "fair prob − price paid" },
            { label: "Buys with edge > 0", value: pct(sc.edge_pos, 0), sub: "per volatility model" },
            { label: "Edge predicts ROI", value: num(sc.edge_spearman, 2, true), sub: "rank corr. across deciles", help: "Near +1 means the model edge lines up with returns." },
          ]} />
          {de.length > 0 && <Card title="ROI by model-edge decile">
            <Bars labels={de.map((r: any) => num(r.edge_mid, 2, true))} values={de.map((r: any) => (r.roi || 0) * 100)} color={signedColor} fmt={(v) => v.toFixed(1) + "%"}
                  tip={(i) => <><div className="k">edge {de[i].edge_bin}</div><b>ROI {pct(de[i].roi, 2)}</b><div className="k">{de[i].trades.toLocaleString()} buys · payoff {money(de[i].payoff)}</div></>} />
          </Card>}
        </>
      ) : <Empty title="No signal tests">They need reference prices covering the trades; none were available.</Empty>}
      <Section title="Hypothesis scorecard" hint="Statistical hints, not proof. The sleeve with large PnL and stable positive ROI is the likely engine." />
      {(S.hypotheses || []).map((h: any) => <div className="hyp" key={h.id}><b>{h.id} · {h.name}</b><br /><span className="t2">{h.text}</span></div>)}
      <Section title="Automation fingerprints" />
      <Kpis items={[
        { label: "Same-second trades", value: pct(S.same_second_share, 0), sub: "of consecutive trades" },
        { label: "Median gap", value: `${(S.median_gap_s || 0).toFixed(0)}s`, sub: "between trades" },
        { label: "Active hours", value: `${S.active_hours || 0}/24`, sub: "UTC hours with trades" },
        { label: "Longest pause", value: `${(S.longest_silence_h || 0).toFixed(1)}h`, sub: "no sleep gap = bot" },
        { label: "Multi-fill clusters", value: pct(S.multi_fill_share, 0), sub: "resting limit orders", help: "Same market/side/price fills within 3s; many suggests maker orders." },
      ]} />
      {S.top_sizes && <p className="hint">Most common buy sizes: {S.top_sizes.map(([k, v]: number[]) => `${k} shares (${Math.round(v * 100)}%)`).join(", ")}</p>}
    </>
  );
}

// ------------------------------------------------------------------ windows
const PAGE = 200;
export function Windows({ t }: { t: any }) {
  const { data } = usePoll<any>(`/api/traders/${t.id}/windows`, 0, [t.summary?.analyzed_at]);
  const [winner, setWinner] = useState<"all" | "up" | "down" | "unresolved">("all");
  const [sides, setSides] = useState<"all" | "two-sided" | "one-sided">("all");
  const [res, setRes] = useState<"all" | "wins" | "losses">("all");
  const [page, setPage] = useState(0);
  const rows: any[] = data?.rows || [];
  const f = useMemo(() => rows.filter((w) =>
    (winner === "all" || (winner === "unresolved" ? !w.winner : w.winner === winner)) &&
    (sides === "all" || !!w.two_sided === (sides === "two-sided")) &&
    (res === "all" || (res === "wins" ? w.pnl > 0 : w.pnl !== null && w.pnl <= 0))), [rows, winner, sides, res]);
  useEffect(() => setPage(0), [winner, sides, res]);
  if (!data) return <p className="hint">Loading…</p>;
  const total = f.reduce((a, w) => a + (w.pnl || 0), 0);
  function csv() {
    const cols = Object.keys(rows[0] || {});
    const text = [cols.join(","), ...f.map((w) => cols.map((c) => w[c] ?? "").join(","))].join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
    a.download = `${t.id}_windows.csv`;
    a.click();
  }
  return (
    <>
      <div className="row wrap" style={{ marginBottom: 12 }}>
        <Seg value={winner} options={["all", "up", "down", "unresolved"] as const} onChange={setWinner} format={(v) => (v === "all" ? "Any winner" : v)} />
        <Seg value={sides} options={["all", "two-sided", "one-sided"] as const} onChange={setSides} format={(v) => (v === "all" ? "Any sides" : v)} />
        <Seg value={res} options={["all", "wins", "losses"] as const} onChange={setRes} format={(v) => (v === "all" ? "Any result" : v)} />
        <span className="spacer" />
        <span className="muted small">{f.length.toLocaleString()} of {rows.length.toLocaleString()} windows · PnL <span className={tone(total)}>{signed(total, 0)}</span> · UTC</span>
        <button className="btn sm" onClick={csv}>Download CSV</button>
      </div>
      <div className="card flush">
        <Table rows={f.slice(page * PAGE, (page + 1) * PAGE)} maxHeight={620} cols={[
          { key: "win_start", label: "Start (UTC)", align: "l", render: (w: any) => dtUTC(w.win_start) },
          { key: "winner", label: "Winner", render: (w: any) => (w.winner ? <Pill side={w.winner} /> : <span className="muted">open</span>) },
          { key: "pnl", label: "PnL", render: (w: any) => <span className={tone(w.pnl)}>{signed(w.pnl)}</span> },
          { key: "cost", label: "Cost", render: (w: any) => money(w.cost, 2) },
          { key: "n_buys", label: "Buys" },
          { key: "up_sh", label: "Up sh", render: (w: any) => num(w.up_sh, 1) },
          { key: "down_sh", label: "Down sh", render: (w: any) => num(w.down_sh, 1) },
          { key: "avg_up", label: "Avg Up", render: (w: any) => num(w.avg_up, 3) },
          { key: "avg_down", label: "Avg Down", render: (w: any) => num(w.avg_down, 3) },
          { key: "pair_cost", label: "Pair cost", render: (w: any) => num(w.pair_cost, 3) },
          { key: "locked_profit", label: "Locked", render: (w: any) => money(w.locked_profit, 2) },
          { key: "redeem_usd", label: "Redeemed", render: (w: any) => money(w.redeem_usd, 2) },
        ]} />
      </div>
      {f.length > PAGE && (
        <div className="row" style={{ marginTop: 10, justifyContent: "center" }}>
          <button className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>Newer</button>
          <span className="muted small">Page {page + 1} of {Math.ceil(f.length / PAGE)}</span>
          <button className="btn sm" disabled={(page + 1) * PAGE >= f.length} onClick={() => setPage(page + 1)}>Older</button>
        </div>
      )}
    </>
  );
}

// ------------------------------------------------------------------ trades (window inspector)
export function Trades({ t }: { t: any }) {
  const { data } = usePoll<any>(`/api/traders/${t.id}/windows`, 0, [t.summary?.analyzed_at]);
  const rows: any[] = data?.rows || [];
  const [slug, setSlug] = useState<string>("");
  const [q, setQ] = useState("");
  useEffect(() => { if (!slug && rows.length) setSlug(rows[0].slug); }, [rows.length]);
  const { data: w } = usePoll<any>(slug ? `/api/traders/${t.id}/window/${slug}` : null, 0, [slug]);
  const opts = rows.filter((r) => !q || dtUTC(r.win_start).includes(q)).slice(0, 400);
  return (
    <>
      <Section title="Window inspector" hint="Reference price path and every buy in the selected window." />
      <div className="row" style={{ marginBottom: 12 }}>
        <input className="input" style={{ width: 200 }} placeholder="Filter: 10-21 14:" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="input" value={slug} onChange={(e) => setSlug(e.target.value)}>
          {opts.map((r) => <option key={r.slug} value={r.slug}>{dtUTC(r.win_start)} UTC · winner {r.winner || "?"} · {r.pnl === null ? "open" : signed(r.pnl)}</option>)}
        </select>
        {slug && <a className="btn ghost sm" href={`https://polymarket.com/event/${slug}`} target="_blank" rel="noreferrer">Open on Polymarket</a>}
      </div>
      {w && <WindowDetail w={w} />}
    </>
  );
}

function WindowDetail({ w }: { w: any }) {
  const buys: any[] = w.buys || [];
  const ups = buys.filter((b) => b.o === "up"), dns = buys.filter((b) => b.o === "down");
  const xs = Array.from(new Set([...ups, ...dns].map((b) => b.t_in))).sort((a: any, b: any) => a - b) as number[];
  const at = (arr: any[]) => { const m = new Map(arr.map((b) => [b.t_in, b.price])); return xs.map((x) => (m.has(x) ? (m.get(x) as number) : null)); };
  return (
    <div className="grid2">
      <Card title={`${w.symbol} (Binance 1s) around the window`}>
        {w.prices.t.length ? <LineChart x={w.prices.t} ys={[w.prices.v]} height={300} legend={false} xFmt={(v) => `${v}s`} yFmt={(v) => v.toLocaleString("en-US", { maximumFractionDigits: 0 })}
          hlines={w.strike ? [{ y: w.strike, color: "rgba(236,235,232,.55)", label: `strike ≈ ${num(w.strike, 2)}` }] : []}
          series={[{ label: w.symbol, color: C.s1, fmt: (v) => num(v, 2) }]} /> : <Empty title="No reference prices">No cached prices cover this window.</Empty>}
      </Card>
      <Card title={<>Buys · winner {w.window?.winner || "?"} · PnL <span className={tone(w.window?.pnl)}>{signed(w.window?.pnl)}</span></>}>
        {xs.length ? <LineChart x={xs} ys={[at(ups), at(dns)]} height={300} xRange={[-5, w.length + 5]} yRange={[0, 1]} xFmt={(v) => `${Math.round(v)}s`} yFmt={(v) => cents(v)}
          series={[{ label: "Buy Up", color: C.up, points: true, fmt: (v) => cents(v) }, { label: "Buy Down", color: C.down, points: true, fmt: (v) => cents(v) }]} /> : <Empty title="No resolved buys in this window" />}
        <p className="hint" style={{ marginTop: 6 }}>{buys.length} buys · x = seconds since window open</p>
      </Card>
    </div>
  );
}

// ------------------------------------------------------------------ report & files, notes
export function ReportFiles({ t, S, onExport }: { t: any; S: any; onExport: () => void }) {
  const [rep, setRep] = useState("");
  const { data: files } = usePoll<any>(`/api/traders/${t.id}/files`, 0, [S?.analyzed_at]);
  useEffect(() => { fetch(`/api/traders/${t.id}/report`).then((r) => r.text()).then(setRep); }, [t.id, S?.analyzed_at]);
  const dl = (name: string, text: string, type = "text/plain") => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name;
    a.click();
  };
  return (
    <div className="grid2" style={{ gridTemplateColumns: "minmax(0,1.5fr) minmax(0,1fr)" }}>
      <Card title="Report">{rep ? <div className="report" style={{ maxHeight: 720, overflow: "auto" }}>{rep}</div> : <p className="hint">No report yet.</p>}</Card>
      <div className="col">
        <Card title="Files on disk" right={<button className="btn primary sm" onClick={onExport}>Export zip</button>}>
          <Table rows={files?.files || []} cols={[{ key: "file", label: "File", align: "l" }, { key: "mb", label: "MB", render: (r: any) => r.mb.toFixed(2) },
            { key: "modified", label: "Modified", render: (r: any) => new Date(r.modified * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) }]} />
          <div className="row" style={{ marginTop: 10 }}>
            {rep && <button className="btn sm" onClick={() => dl(`${t.id}_report.md`, rep)}>report.md</button>}
            {S && <button className="btn sm" onClick={() => dl(`${t.id}_summary.json`, JSON.stringify(S, null, 1), "application/json")}>summary.json</button>}
          </div>
        </Card>
        {files?.log && <Card title="Last job log"><pre className="log">{files.log}</pre></Card>}
      </div>
    </div>
  );
}

export function Notes({ t, onSaved }: { t: any; onSaved: () => void }) {
  const [txt, setTxt] = useState(t.notes || "");
  async function save() {
    try { await api.patch(`/api/traders/${t.id}`, { notes: txt }); toast("Notes saved", "ok"); onSaved(); } catch (e: any) { toast(e.message, "err"); }
  }
  return (
    <Card title="Notes" right={<button className="btn primary sm" disabled={txt === (t.notes || "")} onClick={save}>Save notes</button>}>
      <p className="hint">Hypotheses, things to check next, links. Saved with the trader and included in exports.</p>
      <textarea className="input" rows={16} value={txt} onChange={(e) => setTxt(e.target.value)} />
    </Card>
  );
}

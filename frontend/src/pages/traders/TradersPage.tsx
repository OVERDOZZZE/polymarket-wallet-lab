// Traders: every tracked wallet side by side (was "Compare"), plus overlaid cumulative PnL.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { C, LineChart } from "../../components/charts";
import { Card, Empty, LiveBadge, Seg, Table } from "../../components/ui";
import { api } from "../../lib/api";
import { coverageText, money, num, pct, signed, tone } from "../../lib/format";
import { usePoll } from "../../lib/hooks";
import { AddTrader, ExportDialog } from "./dialogs";

const SERIES = [C.s1, C.s2, C.s3, C.s4, C.s5, "#008300", C.s7, "#e66767"];
const STATUS: Record<string, string> = { downloading: "var(--accent)", error: "var(--down)", ready: "var(--up)", "no trades": "var(--warn)", "no data": "var(--muted)" };

export default function TradersPage() {
  const { data } = usePoll<any>("/api/traders", 4000);
  const nav = useNavigate();
  const [adding, setAdding] = useState(false);
  const [exporting, setExporting] = useState(false);
  const traders: any[] = data?.traders || [];
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Traders</h1>
          <p className="page-sub">Wallets on Polymarket's crypto Up/Down windows: download, reverse-engineer and watch them live.</p>
        </div>
        <span className="spacer" />
        {traders.length > 0 && <button className="btn" onClick={() => setExporting(true)}>Export all</button>}
        <button className="btn primary" onClick={() => setAdding(true)}>Add trader</button>
      </div>
      {data && !traders.length && (
        <div className="grid3">
          {[["1. Add a wallet", "Paste a 0x address, @username or profile link and pick a period."],
            ["2. Read the analysis", "PnL, timing, entry prices, signal tests and a strategy scorecard per wallet."],
            ["3. Watch it live", "Stream its trades, the order book and the settlement price as they happen."]].map(([t, d]) => (
            <div className="card" key={t}><b>{t}</b><p className="hint" style={{ margin: "6px 0 0" }}>{d}</p></div>
          ))}
        </div>
      )}
      {traders.length > 0 && (
        <div className="card flush">
          <Table rows={traders} onRow={(r) => nav(`/traders/${r.id}`)} cols={[
            { key: "name", label: "Trader", align: "l", render: (r) => <div><b>{r.name}</b> {r.watching && <LiveBadge on label="Live" />}<div className="muted small mono">{r.short_wallet}</div></div> },
            { key: "market", label: "Market", render: (r) => `${r.asset} ${r.window_minutes}m` },
            { key: "status", label: "Status", render: (r) => <span className="chip"><span className="dot" style={{ color: STATUS[r.status] }} />{r.status}</span> },
            { key: "data", label: "Data", render: (r) => <span className="muted small">{coverageText(r.coverage)}</span>, sort: (r) => r.coverage[0]?.[0] ?? 0 },
            { key: "windows", label: "Windows", sort: (r) => r.metrics.windows, render: (r) => num(r.metrics.windows, 0) },
            { key: "pnl", label: "PnL", sort: (r) => r.metrics.total_pnl, render: (r) => <span className={tone(r.metrics.total_pnl)}>{signed(r.metrics.total_pnl, 0)}</span> },
            { key: "roi", label: "Return / $", sort: (r) => r.metrics.roi, render: (r) => pct(r.metrics.roi, 2, true) },
            { key: "wr", label: "Windows won", sort: (r) => r.metrics.win_rate_windows, render: (r) => pct(r.metrics.win_rate_windows) },
            { key: "sharpe", label: "Sharpe", sort: (r) => r.metrics.sharpe, render: (r) => num(r.metrics.sharpe, 1) },
            { key: "dd", label: "Max DD", sort: (r) => r.metrics.max_drawdown, render: (r) => money(r.metrics.max_drawdown) },
            { key: "two", label: "Two-sided", sort: (r) => r.metrics.two_sided_share, render: (r) => pct(r.metrics.two_sided_share, 0) },
            { key: "pc", label: "Pair cost", title: "median avg Up + avg Down", sort: (r) => r.metrics.pair_cost_median, render: (r) => num(r.metrics.pair_cost_median, 3) },
            { key: "fav", label: "Buys favourite", sort: (r) => r.metrics.favourite, render: (r) => pct(r.metrics.favourite, 0) },
          ]} />
        </div>
      )}
      {traders.length > 0 && <Compare traders={traders} />}
      {adding && <AddTrader onClose={() => setAdding(false)} />}
      {exporting && <ExportDialog ids={traders.map((t) => t.id)} label={`${traders.length} traders`} onClose={() => setExporting(false)} />}
    </div>
  );
}

function Compare({ traders }: { traders: any[] }) {
  const ready = traders.filter((t) => t.status === "ready");
  const [pick, setPick] = useState<string[]>(() => ready.slice(0, 5).map((t) => t.id));
  const [scale, setScale] = useState<"Dollars" | "Return on deployed">("Dollars");
  const [series, setSeries] = useState<Record<string, any>>({});
  useEffect(() => {
    pick.filter((id) => !series[id]).forEach((id) => api.get(`/api/traders/${id}/pnl`).then((d) => setSeries((s) => ({ ...s, [id]: d }))));
  }, [pick.join(",")]);
  const { x, ys, used } = useMemo(() => {
    const used = traders.map((t, i) => ({ t, color: SERIES[i % SERIES.length] })).filter((u) => pick.includes(u.t.id) && series[u.t.id]?.t?.length);
    const xs = Array.from(new Set(used.flatMap((u) => series[u.t.id].t as number[]))).sort((a, b) => a - b);
    const ys = used.map((u) => {
      const d = series[u.t.id];
      const out: (number | null)[] = new Array(xs.length).fill(null);
      let j = -1;
      xs.forEach((tx, i) => {
        while (j + 1 < d.t.length && d.t[j + 1] <= tx) j++;
        if (j >= 0) out[i] = scale === "Dollars" ? d.cum[j] : d.cost[j] ? (d.cum[j] / d.cost[j]) * 100 : null;
      });
      return out;
    });
    return { x: xs, ys, used };
  }, [series, pick, scale, traders]);
  if (!ready.length) return null;
  return (
    <Card title="Performance over time" style={{ marginTop: 16 }} right={<Seg value={scale} options={["Dollars", "Return on deployed"] as const} onChange={setScale} />}>
      <div className="row wrap" style={{ marginBottom: 10 }}>
        {traders.map((t, i) => (
          <label key={t.id} className="check small">
            <input type="checkbox" checked={pick.includes(t.id)} disabled={t.status !== "ready" || (!pick.includes(t.id) && pick.length >= 8)}
                   onChange={(e) => setPick(e.target.checked ? [...pick, t.id] : pick.filter((p) => p !== t.id))} />
            <span className="sw" style={{ display: "inline-block", width: 10, height: 10, borderRadius: 3, background: SERIES[i % SERIES.length] }} />{t.name}
          </label>
        ))}
      </div>
      {used.length ? (
        <LineChart x={x} ys={ys} time height={360} zero legend={false} series={used.map((u) => ({ label: u.t.name, color: u.color, fmt: (v: number) => scale === "Dollars" ? money(v) : num(v, 2) + "%" }))}
                   yFmt={(v) => (scale === "Dollars" ? "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : v.toFixed(1) + "%")} />
      ) : <Empty title="Pick traders to compare" />}
    </Card>
  );
}

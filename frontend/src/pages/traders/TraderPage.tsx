// One trader: header with actions, running job, and analysis sections.
import { ReactNode, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Alert, Card, Chip, Empty, Progress, Tabs, toast } from "../../components/ui";
import { api } from "../../lib/api";
import { ago, coverageText, shortWallet } from "../../lib/format";
import { usePoll } from "../../lib/hooks";
import { ExportDialog, TraderSettings, UpdateData } from "./dialogs";
import { LiveSection } from "./LiveSection";
import { Notes, Overview, ReportFiles, Signals, TimingPrice, Trades, Windows } from "./sections";

const SECTIONS = ["Overview", "Live", "Timing & price", "Signals", "Windows", "Trades", "Report & files", "Notes"] as const;
const NEEDS_ANALYSIS = new Set(["Overview", "Timing & price", "Signals", "Windows", "Trades"]);

export default function TraderPage() {
  const { id = "" } = useParams();
  const { data: t, error, reload } = usePoll<any>(`/api/traders/${id}`, 2000, [id]);
  const [sec, setSec] = useState<(typeof SECTIONS)[number]>("Overview");
  const [dlg, setDlg] = useState<"" | "update" | "settings" | "export">("");
  if (error) return <div className="page"><Empty title="Trader not found">{error}</Empty></div>;
  if (!t) return <div className="page"><p className="hint">Loading…</p></div>;
  const S = t.summary;
  const job = t.job;
  async function analyse() {
    try { await api.post(`/api/traders/${id}/analyse`); toast("Analysis started", "ok"); reload(); } catch (e: any) { toast(e.message, "err"); }
  }
  let body: ReactNode;
  if (NEEDS_ANALYSIS.has(sec) && (!S || S.empty)) {
    body = !S ? (job ? <Empty title="Downloading">Results appear as soon as the first download and analysis finish.</Empty>
                     : <Empty title="No data yet">Use Update data to download this wallet's activity for a period.</Empty>)
              : <Empty title={`No ${t.asset} ${t.window_minutes}-minute trades found`}>The stored activity has {(S.activity_rows_total || 0).toLocaleString()} rows across other markets. Check the asset and window in Settings, or download a different period.</Empty>;
  } else {
    body = {
      Overview: () => <Overview t={t} S={S} />, Live: () => <LiveSection t={t} />, "Timing & price": () => <TimingPrice t={t} S={S} />,
      Signals: () => <Signals S={S} t={t} />, Windows: () => <Windows t={t} />, Trades: () => <Trades t={t} />,
      "Report & files": () => <ReportFiles t={t} S={S} onExport={() => setDlg("export")} />, Notes: () => <Notes t={t} onSaved={reload} />,
    }[sec]();
  }
  const sum = S?.analyzed_at;
  return (
    <div className="page">
      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div>
          <div className="muted small"><Link to="/traders" style={{ textDecoration: "none" }}>Traders</Link> /</div>
          <h1 className="page-title">{t.name}</h1>
          <div className="row wrap" style={{ gap: 6, marginTop: 6 }}>
            <Chip href={`https://polymarket.com/profile/${t.wallet}`} mono>{shortWallet(t.wallet)}</Chip>
            {t.pm_name && t.pm_name !== t.name && <Chip>@{t.pm_name}</Chip>}
            <Chip>{t.asset} · {t.window_minutes} min windows</Chip>
            <Chip>Data: {coverageText(t.coverage)}</Chip>
            <Chip>{t.updated ? `Updated ${ago(t.updated)}` : "Never updated"}</Chip>
            {t.watching && <Chip dot="var(--up)">Live</Chip>}
            {job && <Chip dot="var(--warn)">{job.kind} running</Chip>}
          </div>
        </div>
        <span className="spacer" />
        <button className="btn primary" disabled={!!job} onClick={() => setDlg("update")}>Update data</button>
        <button className="btn" disabled={!!job} onClick={analyse} title="Re-run the analysis on stored data and pick up late resolutions">Re-analyse</button>
        <button className="btn" onClick={() => setDlg("export")}>Export</button>
        <button className="btn ghost" onClick={() => setDlg("settings")}>Settings</button>
      </div>
      {t.last_error && !job && <div style={{ marginBottom: 12 }}><Alert kind="err">The last job failed: {t.last_error}</Alert></div>}
      {job && (
        <Card style={{ marginBottom: 14 }}>
          <div className="row"><b>{job.kind === "download" ? "Downloading" : "Analysing"}</b><span className="muted">{job.stage} · {Math.round(job.elapsed)}s</span><span className="spacer" />
            <button className="btn sm" onClick={() => api.post(`/api/jobs/${job.id}/cancel`)}>Cancel</button></div>
          <div style={{ margin: "8px 0" }}><Progress frac={job.frac} /></div>
          <details><summary className="muted small">Log</summary><pre className="log">{job.logs.join("\n") || "(empty)"}</pre></details>
        </Card>
      )}
      {!job && t.last_job?.status === "cancelled" && <div style={{ marginBottom: 12 }}><Alert>The last job was cancelled. Everything downloaded before that is kept; run Update data again to continue.</Alert></div>}
      <Tabs value={sec} options={SECTIONS} onChange={setSec} />
      {sum && NEEDS_ANALYSIS.has(sec) && S?.warnings?.length > 0 && <div className="col" style={{ marginBottom: 12, gap: 4 }}>{S.warnings.map((w: string) => <span key={w} className="muted small">{w}</span>)}</div>}
      {body}
      {dlg === "update" && <UpdateData t={t} onClose={() => { setDlg(""); reload(); }} />}
      {dlg === "settings" && <TraderSettings t={t} onClose={() => { setDlg(""); reload(); }} />}
      {dlg === "export" && <ExportDialog ids={[t.id]} label={t.name} onClose={() => setDlg("")} />}
    </div>
  );
}

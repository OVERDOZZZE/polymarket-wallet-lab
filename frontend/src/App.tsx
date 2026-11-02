// App shell: top bar (brand, pill navigation, background jobs, trading status), bottom tab bar on phones (jobs then
// show in the top bar only while one runs), routes.
import { ReactNode, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { Progress, Toasts } from "./components/ui";
import { usePoll } from "./lib/hooks";
import { money } from "./lib/format";
import { api } from "./lib/api";
import TradePage from "./pages/trade/TradePage";
import BotsPage from "./pages/bots/BotsPage";
import BotPage from "./pages/bots/BotPage";
import TradersPage from "./pages/traders/TradersPage";
import TraderPage from "./pages/traders/TraderPage";
import ProfilePage from "./pages/profile/ProfilePage";

const Icon = ({ children }: { children: ReactNode }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round" aria-hidden>{children}</svg>
);
const NAV = [
  { to: "/trade", label: "Trade", icon: <Icon><path d="M3 17l5.5-5.5 4 3.5L21 6.5" /><path d="M15 6.5h6v6" /></Icon> },
  { to: "/bots", label: "Bots", icon: <Icon><rect x="4" y="7.5" width="16" height="12" rx="4" /><path d="M12 3.5v4M9.5 13h.01M14.5 13h.01M10 16.5h4" /></Icon> },
  { to: "/traders", label: "Traders", icon: <Icon><circle cx="9" cy="8.5" r="3.5" /><path d="M2.5 20c.8-3.4 3.3-5.5 6.5-5.5s5.7 2.1 6.5 5.5" /><path d="M15.5 5a3.5 3.5 0 0 1 0 7M18 14.8c1.8.8 2.9 2.5 3.4 5.2" /></Icon> },
  { to: "/profile", label: "Profile", icon: <Icon><circle cx="12" cy="12" r="9" /><circle cx="12" cy="10" r="3.2" /><path d="M6.2 18.4c1.3-2.3 3.4-3.6 5.8-3.6s4.5 1.3 5.8 3.6" /></Icon> },
];

export default function App() {
  const { data } = usePoll<any>("/api/status", 2000);
  const [jobsOpen, setJobsOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setJobsOpen(false), [pathname]);
  const jobs: any[] = data?.jobs || [];
  const running = jobs.filter((j) => j.status === "running").length;
  return (
    <>
      <div className="aura" aria-hidden />
      <TopBar tr={data?.trading} running={running} onJobs={() => setJobsOpen(!jobsOpen)} />
      <Routes>
        <Route path="/" element={<Navigate to="/trade" replace />} />
        <Route path="/trade" element={<TradePage />} />
        <Route path="/bots" element={<BotsPage />} />
        <Route path="/bots/:id" element={<BotPage />} />
        <Route path="/traders" element={<TradersPage />} />
        <Route path="/traders/:id" element={<TraderPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="*" element={<Navigate to="/trade" replace />} />
      </Routes>
      <nav className="tabbar">
        {NAV.map((n) => <NavLink key={n.to} to={n.to}>{n.icon}<span>{n.label}</span></NavLink>)}
      </nav>
      {jobsOpen && <Jobs jobs={jobs} onClose={() => setJobsOpen(false)} />}
      <Toasts />
    </>
  );
}

function TopBar({ tr, running, onJobs }: { tr: any; running: number; onJobs: () => void }) {
  return (
    <header className="topbar">
      <NavLink to="/trade" className="brand">
        <span className="brand-mark" aria-hidden />
        <span className="brand-text">Wallet Lab</span>
      </NavLink>
      <nav className="nav">
        {NAV.map((n) => <NavLink key={n.to} to={n.to}>{n.icon}{n.label}</NavLink>)}
      </nav>
      <span className="spacer" />
      <div className="top-right">
        <button className={`btn ghost sm jobs-btn${running ? " busy" : ""}`} onClick={onJobs} title="Background downloads and analyses">
          {running ? <><span className="dot pulse" style={{ color: "var(--opal-1)" }} />{running} job{running > 1 ? "s" : ""} running</> : "Jobs"}
        </button>
        <NavLink to="/trade" className={`chip trade-chip${tr?.armed ? " armed" : ""}`} title={tr?.reason || "Live trading account"}>
          <span className="dot" style={{ color: !tr?.enabled ? "var(--muted)" : tr.armed ? "var(--down)" : "var(--up)" }} />
          {!tr ? "…" : !tr.enabled ? "Trading off" : tr.armed ? "ARMED" : "Safe"}
          <b className="num">{tr?.usdc !== null && tr?.usdc !== undefined ? money(tr.usdc, 2) : ""}</b>
        </NavLink>
      </div>
    </header>
  );
}

function Jobs({ jobs, onClose }: { jobs: any[]; onClose: () => void }) {
  return (
    <>
      <div className="jobs-scrim" onClick={onClose} />
      <div className="card jobs-pop">
        <div className="card-title">Background jobs
          <span className="spacer" />
          {jobs.some((j) => j.status !== "running") && <button className="btn ghost sm" onClick={() => api.post("/api/jobs/clear")}>Clear</button>}
        </div>
        {!jobs.length && <p className="hint">Nothing running. Downloads and analyses show up here.</p>}
        <div className="col">
          {[...jobs].reverse().map((j) => (
            <div key={j.id}>
              <div className="row small"><b>{j.trader_name}</b><span className="muted">{j.kind}</span><span className="spacer" />
                <span className={j.status === "error" ? "dn" : "muted"}>{j.status === "running" ? `${Math.round(j.frac * 100)}%` : j.status}</span>
                {j.status === "running" && <button className="btn ghost sm" onClick={() => api.post(`/api/jobs/${j.id}/cancel`)}>Cancel</button>}
              </div>
              {j.status === "running" && <><div style={{ margin: "6px 0 4px" }}><Progress frac={j.frac} /></div><div className="muted small">{j.stage}</div></>}
              {j.error && <div className="dn small">{j.error}</div>}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

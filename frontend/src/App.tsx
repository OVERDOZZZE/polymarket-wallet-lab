// App shell: top bar (navigation, background jobs, trading status) + routes.
import { useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { Progress, Toasts } from "./components/ui";
import { usePoll } from "./lib/hooks";
import { money } from "./lib/format";
import { api } from "./lib/api";
import TradePage from "./pages/trade/TradePage";
import BotsPage from "./pages/bots/BotsPage";
import BotPage from "./pages/bots/BotPage";
import TradersPage from "./pages/traders/TradersPage";
import TraderPage from "./pages/traders/TraderPage";

export default function App() {
  return (
    <>
      <TopBar />
      <Routes>
        <Route path="/" element={<Navigate to="/trade" replace />} />
        <Route path="/trade" element={<TradePage />} />
        <Route path="/bots" element={<BotsPage />} />
        <Route path="/bots/:id" element={<BotPage />} />
        <Route path="/traders" element={<TradersPage />} />
        <Route path="/traders/:id" element={<TraderPage />} />
        <Route path="*" element={<Navigate to="/trade" replace />} />
      </Routes>
      <Toasts />
    </>
  );
}

function TopBar() {
  const { data } = usePoll<any>("/api/status", 2000);
  const [open, setOpen] = useState(false);
  const jobs: any[] = data?.jobs || [];
  const active = jobs.filter((j) => j.status === "running");
  const tr = data?.trading;
  return (
    <header className="topbar">
      <NavLink to="/trade" className="brand">
        <span className="brand-mark">
          <svg width="16" height="16" viewBox="0 0 32 32"><path d="M6 23l7-8 5 4 8-10" stroke="#fff" strokeWidth="4" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </span>
        <span className="brand-text">Wallet Lab</span>
      </NavLink>
      <nav className="nav">
        <NavLink to="/trade">Trade</NavLink>
        <NavLink to="/bots">Bots</NavLink>
        <NavLink to="/traders">Traders</NavLink>
      </nav>
      <span className="spacer" />
      <div className="jobs" style={{ position: "relative" }}>
        <button className="btn ghost sm" onClick={() => setOpen(!open)} title="Background downloads and analyses">
          {active.length ? <><span className="dot pulse" style={{ color: "var(--accent)" }} />{active.length} job{active.length > 1 ? "s" : ""} running</> : "Jobs"}
        </button>
        {open && (
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
                  {j.status === "running" && <><Progress frac={j.frac} /><div className="muted small">{j.stage}</div></>}
                  {j.error && <div className="dn small">{j.error}</div>}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
      <NavLink to="/trade" className="chip" style={{ height: 30 }} title={tr?.reason || "Live trading account"}>
        <span className="dot" style={{ color: !tr?.enabled ? "var(--muted)" : tr.armed ? "var(--down)" : "var(--up)" }} />
        {!tr ? "…" : !tr.enabled ? "Trading off" : tr.armed ? "ARMED" : "Safe"}
        <b className="num">{tr?.usdc !== null && tr?.usdc !== undefined ? money(tr.usdc, 2) : ""}</b>
      </NavLink>
    </header>
  );
}

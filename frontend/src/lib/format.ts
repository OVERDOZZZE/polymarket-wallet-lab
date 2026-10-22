// Number/time formatting shared by every page. "–" for missing values.

const bad = (x: unknown): x is null | undefined => x === null || x === undefined || (typeof x === "number" && !isFinite(x));

export function money(x: number | null | undefined, d = 0, signed = false): string {
  if (bad(x)) return "–";
  const s = "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  if (signed) return (x >= 0 ? "+" : "−") + s;
  return (x < 0 ? "−" : "") + s;
}

export const signed = (x: number | null | undefined, d = 2) => money(x, d, true);

export function pct(x: number | null | undefined, d = 1, sign = false): string {
  if (bad(x)) return "–";
  const v = (x * 100).toFixed(d);
  return (sign && x >= 0 ? "+" : "") + v + "%";
}

export function num(x: number | null | undefined, d = 2, sign = false): string {
  if (bad(x)) return "–";
  const s = x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  return sign && x >= 0 ? "+" + s : s;
}

export function cents(p: number | null | undefined): string {
  if (bad(p)) return "–";
  const c = Math.round(p * 1000) / 10; // float noise: 0.58 * 100 = 57.999...
  return (Number.isInteger(c) ? c.toFixed(0) : c.toFixed(1)) + "¢";
}

export const tone = (x: number | null | undefined) => (bad(x) || Math.abs(x) < 1e-9 ? "" : x > 0 ? "up" : "dn");

export const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
export const hms = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
export const dateUTC = (ts: number) => new Date(ts * 1000).toISOString().slice(0, 10);
export const dtUTC = (ts: number) => new Date(ts * 1000).toISOString().slice(5, 16).replace("T", " ");

export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "never";
  const d = Math.max(0, now - ts);
  if (d < 60) return `${d.toFixed(0)}s ago`;
  if (d < 3600) return `${(d / 60).toFixed(0)}m ago`;
  if (d < 86400) return `${(d / 3600).toFixed(0)}h ago`;
  return `${(d / 86400).toFixed(0)}d ago`;
}

export function clock(sec: number): string {
  if (sec <= 0) return "0:00";
  const s = Math.floor(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export const coverageText = (cov: number[][]) =>
  cov.length ? cov.map(([s, e]) => `${dateUTC(s)} to ${dateUTC(e)}`).join(", ") : "no data yet";

export const shortWallet = (w: string) => (w ? `${w.slice(0, 6)}…${w.slice(-4)}` : "");
export const todayUTC = () => new Date().toISOString().slice(0, 10);
export const daysAgoUTC = (n: number) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);

// Shared UI primitives. Pages compose these; styling lives in styles.css.
import { CSSProperties, ReactNode, useEffect, useId, useMemo, useState } from "react";

export function Card({ title, right, children, className = "", style }: {
  title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; style?: CSSProperties;
}) {
  return (
    <div className={`card ${className}`} style={style}>
      {(title || right) && (
        <div className="card-title">
          {title}
          <span className="spacer" />
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

export const Section = ({ title, hint }: { title: string; hint?: string }) => (
  <>
    <div className="sec">{title}</div>
    {hint && <p className="hint">{hint}</p>}
  </>
);

export const Empty = ({ title, children }: { title: string; children?: ReactNode }) => (
  <div className="empty">
    <h3>{title}</h3>
    {children && <p>{children}</p>}
  </div>
);

export const Chip = ({ children, dot, href, mono }: { children: ReactNode; dot?: string; href?: string; mono?: boolean }) => {
  const inner = (
    <>
      {dot && <span className="dot" style={{ color: dot }} />}
      {children}
    </>
  );
  return href ? (
    <a className={`chip${mono ? " mono" : ""}`} href={href} target="_blank" rel="noreferrer">{inner}</a>
  ) : (
    <span className={`chip${mono ? " mono" : ""}`}>{inner}</span>
  );
};

export const LiveBadge = ({ on, label, warn }: { on: boolean; label?: string; warn?: boolean }) => (
  <span className={`live-badge ${warn ? "warn" : on ? "on" : "off"}`}>
    <span className={`dot${on && !warn ? " pulse" : ""}`} />
    {label ?? (on ? "Live" : "Stopped")}
  </span>
);

export function Seg<T extends string | number>({ value, options, onChange, format }: {
  value: T; options: readonly T[]; onChange: (v: T) => void; format?: (v: T) => ReactNode;
}) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={String(o)} className={o === value ? "on" : ""} onClick={() => onChange(o)}>
          {format ? format(o) : String(o)}
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ value, options, onChange, counts }: {
  value: T; options: readonly T[]; onChange: (v: T) => void; counts?: Partial<Record<T, number>>;
}) {
  return (
    <div className="tabs">
      {options.map((o) => (
        <button key={o} className={o === value ? "on" : ""} onClick={() => onChange(o)}>
          {o}
          {counts && counts[o] !== undefined && <span className="count">{counts[o]}</span>}
        </button>
      ))}
    </div>
  );
}

export const Switch = ({ on, onChange, danger, title }: { on: boolean; onChange: (v: boolean) => void; danger?: boolean; title?: string }) => (
  <button className={`switch${on ? " on" : ""}${danger ? " danger" : ""}`} onClick={() => onChange(!on)} title={title} aria-pressed={on} />
);

export const Field = ({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) => (
  <label className="field">
    <span>{label}</span>
    {children}
    {hint && <small>{hint}</small>}
  </label>
);

export const Alert = ({ kind = "info", children }: { kind?: "info" | "err" | "warn"; children: ReactNode }) => (
  <div className={`alert ${kind}`}>{children}</div>
);

export const Progress = ({ frac }: { frac: number }) => (
  <div className="progress"><span style={{ width: `${Math.round(frac * 100)}%` }} /></div>
);

/** Circular progress (Opal's focus ring): opal gradient arc unless `color` is given; children sit in the middle. */
export function Ring({ frac, size = 88, stroke = 8, color, children }: {
  frac: number; size?: number; stroke?: number; color?: string; children?: ReactNode;
}) {
  const id = "ring" + useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const r = (size - stroke) / 2, c = 2 * Math.PI * r, f = Math.min(1, Math.max(0, frac || 0));
  return (
    <div className="ring" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#5ee7ff" /><stop offset=".5" stopColor="#7b6cff" /><stop offset="1" stopColor="#ff6ad5" />
          </linearGradient>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(255,255,255,.08)" strokeWidth={stroke} />
        <circle className="arc" cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke} strokeLinecap="round"
                strokeDasharray={c} strokeDashoffset={c * (1 - f)} transform={`rotate(-90 ${size / 2} ${size / 2})`}
                style={{ stroke: color || `url(#${id})` }} />
      </svg>
      <div className="ring-in">{children}</div>
    </div>
  );
}

export type Kpi = { label: string; value: ReactNode; sub?: ReactNode; tone?: string; help?: string };
export const Kpis = ({ items }: { items: Kpi[] }) => (
  <div className="kpis">
    {items.map((k) => (
      <div className="kpi" key={k.label} title={k.help}>
        <div className="l">{k.label}</div>
        <div className={`v ${k.tone || ""}`}>{k.value}</div>
        <div className="s">{k.sub || " "}</div>
      </div>
    ))}
  </div>
);

export const Strip = ({ items }: { items: Kpi[] }) => (
  <div className="strip">
    {items.map((k) => (
      <div key={k.label} title={k.help}>
        <div className="l">{k.label}</div>
        <div className={`v ${k.tone || ""}`}>{k.value}</div>
        <div className="s">{k.sub}</div>
      </div>
    ))}
  </div>
);

export const Pill = ({ side, children }: { side: string; children?: ReactNode }) => (
  <span className={`pill ${side === "up" ? "up" : "dn"}`}>
    {side === "up" ? "Up" : "Down"}
    {children ? <> {children}</> : null}
  </span>
);

export const BtcMarket = ({ title, sub }: { title: ReactNode; sub?: ReactNode }) => (
  <div className="mkt">
    <div className="mkt-ico">₿</div>
    <div>
      <div className="mkt-t">{title}</div>
      {sub && <div className="mkt-s">{sub}</div>}
    </div>
  </div>
);

// ------------------------------------------------------------------ modal
export function Modal({ title, sub, onClose, children, wide, footer }: {
  title: string; sub?: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean; footer?: ReactNode;
}) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-label={title}>
        <h2>{title}</h2>
        {sub && <p className="hint">{sub}</p>}
        <div className="col" style={{ marginTop: 14 }}>{children}</div>
        {footer && <div className="foot">{footer}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ toasts
type ToastT = { id: number; text: ReactNode; kind: "ok" | "err" | "info" };
let toastSeq = 0;
const toastSubs = new Set<(t: ToastT[]) => void>();
let toastList: ToastT[] = [];
export function toast(text: ReactNode, kind: ToastT["kind"] = "info", ms = 4000) {
  const t = { id: ++toastSeq, text, kind };
  toastList = [...toastList, t].slice(-5);
  toastSubs.forEach((f) => f(toastList));
  setTimeout(() => {
    toastList = toastList.filter((x) => x.id !== t.id);
    toastSubs.forEach((f) => f(toastList));
  }, ms);
}
export function Toasts() {
  const [list, setList] = useState<ToastT[]>([]);
  useEffect(() => {
    toastSubs.add(setList);
    return () => { toastSubs.delete(setList); };
  }, []);
  return (
    <div className="toasts">
      {list.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>)}
    </div>
  );
}

// ------------------------------------------------------------------ data table
export type Col<T> = {
  key: string; label: ReactNode; render?: (r: T) => ReactNode; sort?: (r: T) => number | string | null;
  align?: "l" | "r"; title?: string;
};
export function Table<T>({ rows, cols, maxHeight, onRow, initialSort, empty, footer }: {
  rows: T[]; cols: Col<T>[]; maxHeight?: number; onRow?: (r: T) => void;
  initialSort?: { key: string; desc: boolean }; empty?: ReactNode; footer?: ReactNode;
}) {
  const [sort, setSort] = useState(initialSort || null);
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const c = cols.find((x) => x.key === sort.key);
    if (!c) return rows;
    const get = c.sort || ((r: any) => r[c.key]);
    return [...rows].sort((a, b) => {
      const x = get(a), y = get(b);
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x < y ? -1 : 1) * (sort.desc ? -1 : 1);
    });
  }, [rows, cols, sort]);
  if (!rows.length && empty) return <>{empty}</>;
  return (
    <div className="tbl-wrap" style={maxHeight ? { maxHeight } : undefined}>
      <table className="pm">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.key} className={`sortable ${c.align === "l" ? "l" : ""}`} title={c.title}
                  onClick={() => setSort(sort && sort.key === c.key ? { key: c.key, desc: !sort.desc } : { key: c.key, desc: true })}>
                {c.label}{sort?.key === c.key ? (sort.desc ? " ↓" : " ↑") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => (
            <tr key={i} className={onRow ? "click" : ""} onClick={onRow ? () => onRow(r) : undefined}>
              {cols.map((c) => (
                <td key={c.key} className={c.align === "l" ? "l" : ""}>{c.render ? c.render(r) : String((r as any)[c.key] ?? "–")}</td>
              ))}
            </tr>
          ))}
        </tbody>
        {footer}
      </table>
    </div>
  );
}

export function download(url: string) {
  const a = document.createElement("a");
  a.href = url;
  a.click();
}

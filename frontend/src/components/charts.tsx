// Charts. uPlot (canvas, very fast) for anything on a numeric/time x-axis; a small SVG bar chart for
// categories. Thin 2px lines, recessive grid, hover tooltip on every chart, legend for >= 2 series.
import { ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

export const C = {
  s1: "#3987e5", s2: "#d95926", s3: "#199e70", s4: "#c98500", s5: "#d55181", s7: "#9085e9",
  up: "#30d158", down: "#ff453a", pnl: "#9b7bff", muted: "#8e8e93", grid: "rgba(255,255,255,0.05)",
  axis: "#8e8e93", text: "#f5f5f7", avg: "rgba(245,245,247,0.85)",
};

const rgba = (hex: string, a: number) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
};
/** Area fill that fades from the line colour to nothing at the bottom of the plot. */
const fadeFill = (hex: string) => (u: uPlot) => {
  const g = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
  g.addColorStop(0, rgba(hex, 0.34));
  g.addColorStop(1, rgba(hex, 0));
  return g;
};

/** Centred moving average over a time window of `frac` x the x span (uneven x spacing is fine; nulls skipped). */
export function movingAvg(x: ArrayLike<number>, y: ArrayLike<number | null>, frac = 0.05): (number | null)[] {
  const n = x.length, out: (number | null)[] = new Array(n).fill(null);
  const half = n > 2 ? ((x[n - 1] - x[0]) * frac) / 2 : 0;
  if (half <= 0) return out;
  let lo = 0, hi = 0, sum = 0, cnt = 0;
  for (let i = 0; i < n; i++) {
    for (; hi < n && x[hi] <= x[i] + half; hi++) { const v = y[hi]; if (v !== null && v !== undefined) { sum += v; cnt++; } }
    for (; x[lo] < x[i] - half; lo++) { const v = y[lo]; if (v !== null && v !== undefined) { sum -= v; cnt--; } }
    out[i] = cnt ? sum / cnt : null;
  }
  return out;
}

export type LSeries = {
  label: string; color: string; width?: number; fill?: string; gradient?: boolean; points?: boolean; pointSize?: number;
  dash?: number[]; step?: boolean; show?: boolean; fmt?: (v: number) => string;
};

type Props = {
  x: ArrayLike<number>; ys: (ArrayLike<number | null>)[]; series: LSeries[]; height?: number;
  time?: boolean; xFmt?: (v: number) => string; yFmt?: (v: number) => string;
  hlines?: { y: number; color: string; label?: string }[]; yRange?: [number, number]; xRange?: [number, number];
  zero?: boolean; legend?: boolean; className?: string; onReady?: (u: uPlot) => void;
  /** overlay a moving average of every series (true = window of 5% of the x span, or that fraction) */
  smooth?: boolean | number;
};

/** Tooltip plugin: crosshair readout at the hovered x. */
function tooltip(series: LSeries[], xFmt: (v: number) => string) {
  let el: HTMLDivElement;
  return {
    hooks: {
      init: (u: uPlot) => {
        el = document.createElement("div");
        el.className = "chart-tip";
        el.style.display = "none";
        u.over.appendChild(el);
        u.over.addEventListener("mouseleave", () => (el.style.display = "none"));
      },
      setCursor: (u: uPlot) => {
        const i = u.cursor.idx;
        if (i === null || i === undefined || u.cursor.left === undefined || u.cursor.left < 0) {
          el.style.display = "none";
          return;
        }
        const rows = series.map((s, k) => {
          const v = u.data[k + 1][i];
          if (v === null || v === undefined || u.series[k + 1].show === false) return "";
          const f = s.fmt ? s.fmt(v as number) : (v as number).toLocaleString("en-US", { maximumFractionDigits: 2 });
          return `<div><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};margin-right:6px"></span><span class="k">${s.label}</span> <b>${f}</b></div>`;
        }).join("");
        if (!rows) { el.style.display = "none"; return; }
        el.innerHTML = `<div class="k">${xFmt(u.data[0][i] as number)}</div>${rows}`;
        el.style.display = "block";
        const w = el.offsetWidth;
        const left = u.cursor.left! + 14 + w > u.over.clientWidth ? u.cursor.left! - w - 14 : u.cursor.left! + 14;
        el.style.left = left + "px";
        el.style.top = Math.max(0, (u.cursor.top || 0) - 10) + "px";
      },
    },
  };
}

/** Dashed horizontal reference lines (e.g. price to beat). */
function hlinePlugin(get: () => { y: number; color: string; label?: string }[]) {
  return {
    hooks: {
      draw: (u: uPlot) => {
        const ctx = u.ctx;
        for (const h of get()) {
          const y = u.valToPos(h.y, "y", true);
          if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) continue;
          ctx.save();
          ctx.strokeStyle = h.color;
          ctx.setLineDash([6 * devicePixelRatio, 5 * devicePixelRatio]);
          ctx.lineWidth = 1.2 * devicePixelRatio;
          ctx.beginPath();
          ctx.moveTo(u.bbox.left, y);
          ctx.lineTo(u.bbox.left + u.bbox.width, y);
          ctx.stroke();
          if (h.label) {
            ctx.setLineDash([]);
            ctx.textAlign = "left";
            ctx.textBaseline = "alphabetic";
            ctx.font = `${11 * devicePixelRatio}px Inter Variable, sans-serif`;
            ctx.fillStyle = h.color;
            ctx.fillText(h.label, u.bbox.left + 6 * devicePixelRatio, y - 5 * devicePixelRatio);
          }
          ctx.restore();
        }
      },
    },
  };
}

const defaultTimeFmt = (v: number) => new Date(v * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

export function LineChart(p: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const hl = useRef(p.hlines || []);
  hl.current = p.hlines || [];
  const xFmt = p.xFmt || (p.time ? defaultTimeFmt : (v: number) => String(v));
  const frac = p.smooth === true ? 0.05 : p.smooth || 0;
  const series: LSeries[] = frac ? [...p.series, ...p.series.map((s) => ({ label: `${s.label} (avg)`, color: C.avg, width: 1.75, show: s.show, fmt: s.fmt }))] : p.series;
  const ys = useMemo(() => (frac ? [...p.ys, ...p.ys.map((y) => movingAvg(p.x, y, frac))] : p.ys), [p.x, p.ys, frac]);
  const key = series.map((s) => s.label + s.color + (s.show === false)).join("|") + (p.time ? "t" : "n") + (p.yRange || "") + (p.xRange || "");

  useLayoutEffect(() => {
    const el = ref.current!;
    const opts: uPlot.Options = {
      width: el.clientWidth || 600,
      height: p.height || 280,
      padding: [10, 8, 0, 4],
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 7 } },
      scales: {
        x: { time: !!p.time, ...(p.xRange ? { range: () => p.xRange! } : {}) },
        y: p.yRange ? { range: () => p.yRange! } : {},
      },
      axes: [
        { stroke: C.axis, grid: { show: false }, ticks: { stroke: C.grid, size: 4 }, font: "11px Inter Variable, sans-serif",
          ...(p.time ? {} : { values: (_u: uPlot, vs: number[]) => vs.map((v) => (p.xFmt ? p.xFmt(v) : String(v))) }) },
        { stroke: C.axis, grid: { stroke: C.grid, width: 1 }, ticks: { show: false }, size: 62, font: "11px Inter Variable, sans-serif",
          values: (_u: uPlot, vs: number[]) => vs.map((v) => (p.yFmt ? p.yFmt(v) : v.toLocaleString("en-US"))) },
      ],
      series: [
        {},
        ...series.map((s) => ({
          label: s.label, stroke: s.color, width: s.width ?? 2, fill: s.gradient ? fadeFill(s.color) : s.fill, show: s.show !== false, dash: s.dash,
          spanGaps: true,
          paths: s.points ? () => null : s.step ? uPlot.paths.stepped!({ align: 1 }) : undefined,
          points: s.points ? { show: true, size: s.pointSize ?? 9, fill: s.color, stroke: "#111114", width: 1.5 } : { show: false },
        })),
      ],
      plugins: [tooltip(series, xFmt), hlinePlugin(() => [...hl.current, ...(p.zero ? [{ y: 0, color: "rgba(140,139,133,.5)" }] : [])])],
    };
    const u = new uPlot(opts, [Array.from(p.x), ...ys.map((y) => Array.from(y))] as any, el);
    plot.current = u;
    p.onReady?.(u);
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: p.height || 280 }));
    ro.observe(el);
    return () => { ro.disconnect(); u.destroy(); plot.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, p.height]);

  useEffect(() => {
    plot.current?.setData([Array.from(p.x), ...ys.map((y) => Array.from(y))] as any);
  }, [p.x, ys]);

  return (
    <div className={p.className}>
      {p.legend !== false && series.length > 1 && (
        <div className="legend" style={{ marginBottom: 6 }}>
          {series.map((s) => <span key={s.label}><span className="sw" style={{ background: s.color }} />{s.label}</span>)}
        </div>
      )}
      <div ref={ref} className="chart" />
    </div>
  );
}

// ------------------------------------------------------------------ category bars (SVG)
export function Bars({ labels, values, height = 240, fmt = (v) => v.toLocaleString("en-US"), color, tip }: {
  labels: string[]; values: number[]; height?: number; fmt?: (v: number) => string;
  color?: (v: number, i: number) => string; tip?: (i: number) => ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(600);
  const [hover, setHover] = useState<number | null>(null);
  useLayoutEffect(() => {
    const el = ref.current!;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  const padL = 56, padB = 34, padT = 10;
  const ih = height - padB - padT, iw = Math.max(10, w - padL - 6);
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  const span = hi - lo || 1;
  const yOf = (v: number) => padT + ih - ((v - lo) / span) * ih;
  const bw = iw / Math.max(1, values.length);
  const ticks = useMemo(() => {
    const step = niceStep(span / 4);
    const out = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) out.push(t);
    return out;
  }, [lo, hi, span]);
  const col = color || ((v: number) => (v >= 0 ? C.s1 : C.down));
  const every = Math.ceil(labels.length / Math.max(1, Math.floor(iw / 46)));
  return (
    <div ref={ref} className="chart" onMouseLeave={() => setHover(null)}>
      <svg width={w} height={height} className="bars-svg">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={w - 4} y1={yOf(t)} y2={yOf(t)} stroke={t === 0 ? "rgba(140,139,133,.5)" : C.grid} />
            <text x={padL - 8} y={yOf(t) + 3} textAnchor="end">{fmt(t)}</text>
          </g>
        ))}
        {values.map((v, i) => {
          const x = padL + i * bw + Math.min(2, bw * 0.1);
          const y0 = yOf(0), y1 = yOf(v);
          const h = Math.max(1, Math.abs(y1 - y0));
          return (
            <g key={i} onMouseEnter={() => setHover(i)}>
              <rect x={padL + i * bw} y={padT} width={bw} height={ih} fill="transparent" />
              <rect x={x} y={Math.min(y0, y1)} width={Math.max(1, bw - Math.min(4, bw * 0.2))} height={h} rx={Math.min(6, bw / 3)}
                    fill={col(v, i)} opacity={hover === null || hover === i ? 1 : 0.55} />
              {i % every === 0 && <text x={padL + i * bw + bw / 2} y={height - padB + 16} textAnchor="middle">{labels[i]}</text>}
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="chart-tip" style={{ left: Math.min(w - 180, padL + hover * bw + bw / 2 + 8), top: 8 }}>
          {tip ? tip(hover) : <><div className="k">{labels[hover]}</div><b>{fmt(values[hover])}</b></>}
        </div>
      )}
    </div>
  );
}

function niceStep(raw: number) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const n = raw / p;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * p;
}

/** Histogram -> Bars over `bins` equal-width bins between the 1st and 99th percentile. */
export function histogram(values: number[], bins = 50) {
  if (!values.length) return { labels: [], counts: [], edges: [] as number[] };
  const s = [...values].sort((a, b) => a - b);
  const lo = s[Math.floor(s.length * 0.01)], hi = s[Math.min(s.length - 1, Math.floor(s.length * 0.99))];
  const w = (hi - lo) / bins || 1;
  const counts = new Array(bins).fill(0);
  for (const v of values) counts[Math.min(bins - 1, Math.max(0, Math.floor((v - lo) / w)))]++;
  const edges = counts.map((_, i) => lo + i * w);
  return { labels: edges.map((e) => e.toFixed(0)), counts, edges };
}

// Live BTC chart: Chainlink + its rolling 60 s TWAP (what the market settles on) + Coinbase / Kraken / Binance,
// redrawn every animation frame straight from the feed buffers (no React re-render per tick). Price to beat as a
// dashed line.
import { useEffect, useLayoutEffect, useRef } from "react";
import uPlot from "uplot";
import { C } from "../../components/charts";
import { FEED_HEX, FEEDS, FeedId, TWAP_S, feeds, subscribe } from "../../lib/feeds";

const TWAP_HEX = C.s7;
const TWAP_LABEL = "60 s TWAP (settles)";

const SPANS: Record<string, number> = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800 };

export function PriceChart({ span, visible, align, strike, windowStart, windowEnd, height = 360 }: {
  span: string; visible: Record<FeedId, boolean>; align: boolean; strike: number | null;
  windowStart?: number; windowEnd?: number; height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const cfg = useRef({ span, visible, align, strike, windowStart, windowEnd });
  cfg.current = { span, visible, align, strike, windowStart, windowEnd };

  useLayoutEffect(() => {
    const el = ref.current!;
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.style.display = "none";
    const opts: uPlot.Options = {
      width: el.clientWidth || 800, height, padding: [12, 8, 0, 0], legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 6 } },
      scales: { x: { time: true }, y: { range: (_u, lo, hi) => {
        const s = cfg.current.strike;
        if (s !== null && s !== undefined) { lo = Math.min(lo, s); hi = Math.max(hi, s); }
        const pad = Math.max((hi - lo) * 0.12, 2);
        return [lo - pad, hi + pad];
      } } },
      axes: [
        { stroke: C.axis, grid: { show: false }, ticks: { stroke: C.grid, size: 4 }, font: "11px Inter Variable, sans-serif" },
        { side: 1, stroke: C.axis, grid: { stroke: C.grid }, ticks: { show: false }, size: 74, font: "11px Inter Variable, sans-serif",
          values: (_u, vs) => vs.map((v) => "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 })) },
      ],
      series: [{}, ...FEEDS.map((f) => ({ label: f.label, stroke: FEED_HEX[f.id], width: f.id === "chainlink" ? 2 : 1.6, spanGaps: true, points: { show: false } })),
               { label: TWAP_LABEL, stroke: TWAP_HEX, width: 2.6, spanGaps: true, points: { show: false } }],
      hooks: {
        init: [(u) => { u.over.appendChild(tip); u.over.addEventListener("mouseleave", () => (tip.style.display = "none")); }],
        setCursor: [(u) => {
          const i = u.cursor.idx;
          if (i === null || i === undefined || (u.cursor.left ?? -1) < 0) { tip.style.display = "none"; return; }
          const t = new Date((u.data[0][i] as number) * 1000).toLocaleTimeString([], { hour12: false });
          const lines = [...FEEDS.map((f) => [f.label, FEED_HEX[f.id]]), [TWAP_LABEL, TWAP_HEX]];
          tip.innerHTML = `<div class="k">${t}</div>` + lines.map(([label, hex], k) => {
            const v = u.data[k + 1][i];
            return v === null || v === undefined || !u.series[k + 1].show ? "" :
              `<div><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${hex};margin-right:6px"></span><span class="k">${label}</span> <b>$${(v as number).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</b></div>`;
          }).join("");
          tip.style.display = "block";
          const w = tip.offsetWidth, l = u.cursor.left!;
          tip.style.left = (l + 14 + w > u.over.clientWidth ? l - w - 14 : l + 14) + "px";
          tip.style.top = "8px";
        }],
        draw: [(u) => {
          const { strike: s, windowStart: ws, windowEnd: we } = cfg.current;
          const ctx = u.ctx, dpr = devicePixelRatio;
          ctx.save();
          // window boundaries
          for (const t of [ws, we]) {
            if (!t) continue;
            const x = u.valToPos(t, "x", true);
            if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
            ctx.strokeStyle = "rgba(255,255,255,.12)";
            ctx.lineWidth = dpr;
            ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke();
          }
          if (s !== null && s !== undefined) {
            const y = u.valToPos(s, "y", true);
            ctx.strokeStyle = "#ecebe8";
            ctx.globalAlpha = 0.55;
            ctx.setLineDash([6 * dpr, 5 * dpr]);
            ctx.lineWidth = 1.2 * dpr;
            ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
            ctx.setLineDash([]);
            ctx.globalAlpha = 1;
            ctx.fillStyle = "#ecebe8";
            ctx.textAlign = "left";
            ctx.textBaseline = "alphabetic";
            ctx.font = `${600} ${11 * dpr}px Inter Variable, sans-serif`;
            ctx.fillText("Price to beat", u.bbox.left + 6 * dpr, y - 6 * dpr);
          }
          // direct labels: last value of each visible line at the right edge
          [...FEEDS.map((f) => FEED_HEX[f.id]), TWAP_HEX].forEach((hex, k) => {
            const ys = u.data[k + 1];
            if (!u.series[k + 1].show || !ys.length) return;
            const v = ys[ys.length - 1];
            if (v === null || v === undefined) return;
            const y = u.valToPos(v as number, "y", true);
            ctx.fillStyle = hex;
            ctx.beginPath(); ctx.arc(u.bbox.left + u.bbox.width - 2 * dpr, y, 3.5 * dpr, 0, Math.PI * 2); ctx.fill();
          });
          ctx.restore();
        }],
      },
    };
    const u = new uPlot(opts, [[], [], [], [], [], []] as any, el);
    plot.current = u;
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    let lastDraw = 0;
    const off = subscribe(() => {
      const now = performance.now();
      if (now - lastDraw < 33) return; // ~30 fps is plenty for a price line
      lastDraw = now;
      redraw(u);
    });
    redraw(u);
    return () => { off(); ro.disconnect(); u.destroy(); };
  }, [height]);

  useEffect(() => {
    const u = plot.current;
    if (!u) return;
    FEEDS.forEach((f, k) => u.setSeries(k + 1, { show: visible[f.id] }));
    redraw(u);
  }, [visible, align, span, strike]);

  function redraw(u: uPlot) {
    const { span: sp, visible: vis, align: al } = cfg.current;
    const spanS = SPANS[sp] || 300;
    const step = spanS <= 300 ? 100 : spanS <= 900 ? 250 : 500; // ms grid
    const nowMs = Date.now();
    const t0 = Math.floor((nowMs - spanS * 1000) / step) * step;
    const n = Math.floor((nowMs - t0) / step) + 1;
    const xs = new Array(n);
    for (let i = 0; i < n; i++) xs[i] = (t0 + i * step) / 1000;
    const cols = FEEDS.map((f) => {
      const out = new Array(n).fill(null);
      if (!vis[f.id]) return out;
      const fd = feeds[f.id];
      const add = al && f.id !== "chainlink" ? fd.basis : 0;
      let j = 0;
      // first point at or before t0 (carry-in)
      let lo = 0, hi = fd.t.length - 1, start = -1;
      while (lo <= hi) { const mid = (lo + hi) >> 1; if (fd.t[mid] <= t0) { start = mid; lo = mid + 1; } else hi = mid - 1; }
      let cur = start >= 0 ? fd.v[start] + add : null;
      j = start + 1;
      for (let i = 0; i < n; i++) {
        const edge = t0 + i * step;
        while (j < fd.t.length && fd.t[j] <= edge) { cur = fd.v[j] + add; j++; }
        out[i] = cur;
      }
      return out;
    });
    u.setData([xs, ...cols, twapCol(t0, step, n)] as any);
  }

  return <div ref={ref} className="chart" />;
}

/** Rolling 60 s TWAP of the Chainlink line on the chart's time grid: mean of the ticks in (edge - 60 s, edge]
 *  (same rule as feeds.twapAt; on receive time, like the Chainlink line it smooths). Needs 50 s of ticks. */
function twapCol(t0: number, step: number, n: number) {
  const fd = feeds.chainlink, out = new Array(n).fill(null), W = TWAP_S * 1000;
  let lo = 0, hi = 0, sum = 0;
  while (lo < fd.t.length && fd.t[lo] <= t0 - W) lo++;
  hi = lo;
  for (let i = 0; i < n; i++) {
    const edge = t0 + i * step;
    while (hi < fd.t.length && fd.t[hi] <= edge) sum += fd.v[hi++];
    while (lo < hi && fd.t[lo] <= edge - W) sum -= fd.v[lo++];
    const cnt = hi - lo;
    if (cnt >= 50 && fd.t[lo] - (edge - W) < 12000) out[i] = sum / cnt;
  }
  return out;
}

export const SPAN_OPTIONS = Object.keys(SPANS);

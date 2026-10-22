// Tiny value-over-time line for cards: just the line, no axes or baseline. Green when the range ended above
// where it started, red when below (gain/loss); hover shows the value and time at that point.
import { useMemo, useRef, useState } from "react";
import { C } from "./charts";
import { money } from "../lib/format";

export function Sparkline({ points, height = 56, label }: { points: [number, number][]; height?: number; label?: string }) {
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const W = 300, H = height, PAD = 4;
  const geo = useMemo(() => {
    if (points.length < 2) return null;
    const t0 = points[0][0], t1 = points[points.length - 1][0];
    let lo = Infinity, hi = -Infinity;
    for (const [, v] of points) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const x = (t: number) => ((t - t0) / Math.max(t1 - t0, 1)) * W;
    const y = (v: number) => PAD + (1 - (v - lo) / (hi - lo)) * (H - 2 * PAD);
    const d = points.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
    return { x, y, d, base: points[0][1], last: points[points.length - 1][1] };
  }, [points, H]);
  if (!geo) return <div className="spark-empty muted small">No history yet</div>;
  const color = geo.last >= geo.base ? C.up : C.down;
  const hp = hover !== null ? points[hover] : null;

  function move(e: React.MouseEvent) {
    const r = ref.current!.getBoundingClientRect();
    const tx = ((e.clientX - r.left) / r.width) * W;
    let best = 0, bd = Infinity;
    points.forEach(([t], i) => { const d = Math.abs(geo!.x(t) - tx); if (d < bd) { bd = d; best = i; } });
    setHover(best);
  }

  return (
    <div className="spark">
      <div className="spark-head">
        <span className="muted small">{hp ? new Date(hp[0] * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : label}</span>
        {hp && <span className="small num">{money(hp[1], 2)}</span>}
      </div>
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" width="100%" height={H}
           onMouseMove={move} onMouseLeave={() => setHover(null)} role="img"
           aria-label={`${label || "Value"}: ${money(geo.base, 2)} to ${money(geo.last, 2)}`}>
        <path d={geo.d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        {hp && <line x1={geo.x(hp[0])} x2={geo.x(hp[0])} y1={0} y2={H} stroke={C.text} strokeOpacity={0.3} vectorEffect="non-scaling-stroke" />}
      </svg>
    </div>
  );
}

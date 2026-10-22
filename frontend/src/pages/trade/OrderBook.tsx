// Polymarket-style order book for one outcome. Click a level to use its price in a limit order.
import { bestAsk, bestBid, books, levels } from "../../lib/feeds";
import { cents, money } from "../../lib/format";
import { useFeedTick } from "./useTrade";

export function OrderBook({ token, outcome, myPrices, onPick, depth = 8 }: {
  token?: string; outcome: "up" | "down"; myPrices: Set<number>; onPick: (p: number) => void; depth?: number;
}) {
  useFeedTick(8);
  const b = token ? books.get(token) : undefined;
  if (!b || !b.ok) return <div className="hint" style={{ padding: 14 }}>Connecting to the order book…</div>;
  const asks = levels(b.asks, true, depth).reverse();
  const bids = levels(b.bids, false, depth);
  const maxSz = Math.max(1, ...asks.map((x) => x[1]), ...bids.map((x) => x[1]));
  const cum = (arr: [number, number][], fromEnd: boolean) => {
    const out: number[] = new Array(arr.length);
    let s = 0;
    const idx = arr.map((_, i) => i);
    for (const i of fromEnd ? idx.reverse() : idx) { s += arr[i][0] * arr[i][1]; out[i] = s; }
    return out;
  };
  const askCum = cum(asks, true), bidCum = cum(bids, false);
  const bb = bestBid(b), ba = bestAsk(b);
  const row = (kind: "ask" | "bid", [px, sz]: [number, number], total: number) => (
    <div key={kind + px} className={`book-row ${kind} click`} onClick={() => onPick(px)} title="Use this price">
      <div className="bar" style={{ width: `${(sz / maxSz) * 100}%` }} />
      <span className="px" style={{ textAlign: "left" }}>{myPrices.has(Math.round(px * 1000) / 1000) && <span className="mine" />}{cents(px)}</span>
      <span>{sz.toLocaleString("en-US", { maximumFractionDigits: 0 })}</span>
      <span className="muted">{money(px * sz, 0)}</span>
      <span className="muted">{money(total, 0)}</span>
    </div>
  );
  return (
    <div className="book">
      <div className="book-row book-head"><span style={{ textAlign: "left" }}>Trade {outcome === "up" ? "Up" : "Down"} · price</span><span>Shares</span><span>Value</span><span>Total</span></div>
      {asks.map((l, i) => row("ask", l, askCum[i]))}
      <div className="book-mid">
        <span>Last {cents(b.last)}</span>
        <span>Spread {bb !== null && ba !== null ? cents(ba - bb) : "–"}</span>
      </div>
      {bids.map((l, i) => row("bid", l, bidCum[i]))}
    </div>
  );
}

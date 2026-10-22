// Polymarket-style "Positions" card for the window on screen: what we hold of Up / Down, live value at the
// best bid, cost, return, and a Sell button that loads the order ticket with the whole position.
import { Pill, Table } from "../../components/ui";
import { cents, money, num, pct, signed, tone } from "../../lib/format";
import { floor2, heldShares, livePosition } from "./positions";
import { Account, Market } from "./useTrade";

export function WindowPositions({ m, acct, onSell, onAll }: {
  m: Market | null; acct: Account; onSell: (o: "up" | "down", shares: number) => void; onAll: () => void;
}) {
  if (!m) return null;
  const rows = (["up", "down"] as const)
    .map((o) => ({ o, held: heldShares(acct, m.tokens[o]) }))
    .filter((r) => r.held >= 0.01)
    .map((r) => ({ ...r, ...livePosition(acct, m.tokens[r.o], r.held) }));
  if (!rows.length) return null;
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 6 }}>
        <div className="pos-title">Positions</div>
        <span className="spacer" />
        <button className="btn sm" onClick={onAll}>View all positions</button>
      </div>
      <Table rows={rows} cols={[
        { key: "o", label: "Outcome", align: "l", render: (r) => <Pill side={r.o} /> },
        { key: "shares", label: "Qty", align: "l", render: (r) => num(r.shares, 2) },
        { key: "avg", label: "Avg", align: "l", render: (r) => cents(r.avg) },
        { key: "value", label: "Value", align: "l", render: (r) => (
          <div><b>{money(r.value, 2)}</b><div className="muted small">{r.cost !== null ? `Cost ${money(r.cost, 2)}` : "Cost pending"}</div></div>) },
        { key: "pnl", label: "Return", align: "l", title: "Value at the best bid now minus what you paid", render: (r) => r.pnl === null ? <span className="muted">–</span> : (
          <span className={tone(r.pnl)}><b>{signed(r.pnl)}</b> ({pct(r.pct, 2, true)})</span>) },
        { key: "x", label: "", render: (r) => (
          <button className="btn sm" disabled={r.shares < 0.01} onClick={() => onSell(r.o, floor2(r.shares))}>Sell</button>) },
      ]} />
    </div>
  );
}

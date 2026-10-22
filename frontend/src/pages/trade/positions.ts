// Live profit/loss of the shares we hold: cost from Polymarket's position data (average price), value at the
// best bid of the live book in this tab (what selling now would get at the top of the book).
import { bestBid, books } from "../../lib/feeds";
import { Account } from "./useTrade";

export type LivePos = {
  token: string; shares: number; avg: number | null; cost: number | null;
  px: number | null; value: number | null; pnl: number | null; pct: number | null;
};

export const floor2 = (x: number) => Math.floor(x * 100 + 1e-6) / 100;

/** Shares held of a token. The wallet balance updates seconds after a fill settles and Polymarket's position
 *  list a little later, so take whichever already shows them. */
export function heldShares(acct: Account, token: string | undefined) {
  if (!token) return 0;
  const p = acct.positions.find((x) => String(x.asset) === token);
  return Math.max(acct.tokens[token] ?? 0, p ? Number(p.size) || 0 : 0);
}

export function livePosition(acct: Account, token: string, shares = heldShares(acct, token)): LivePos {
  const p = acct.positions.find((x) => String(x.asset) === token);
  const avg = p && Number(p.avgPrice) > 0 ? Number(p.avgPrice) : null;
  const bid = bestBid(books.get(token));
  const px = bid ?? (p && p.curPrice != null ? Number(p.curPrice) : null);
  const value = px !== null ? shares * px : null;
  const cost = avg !== null ? shares * avg : null;
  const pnl = value !== null && cost !== null ? value - cost : null;
  return { token, shares, avg, cost, px, value, pnl, pct: pnl !== null && cost ? pnl / cost : null };
}

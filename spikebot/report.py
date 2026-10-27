"""python -m spikebot.report : paper account summary from logs/spikebot.db."""
import sqlite3
import argparse
import time

from . import config as C


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="main", choices=sorted(C.VARIANTS))
    C.apply_variant(ap.parse_args().variant)
    db = sqlite3.connect(str(C.DB_PATH))
    q = lambda s, *a: db.execute(s, a).fetchall()
    spent = q("SELECT COALESCE(SUM(cost + fee), 0) FROM trades")[0][0]
    paid = q("SELECT COALESCE(SUM(payout), 0) FROM windows WHERE winner IS NOT NULL")[0][0]
    cash = C.BANKROLL_USD - spent + paid
    n, sh, cost, fee = q("SELECT COUNT(*), COALESCE(SUM(shares),0), COALESCE(SUM(cost),0), COALESCE(SUM(fee),0) FROM trades")[0]
    sw = q("SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(SUM(pnl > 0),0), COALESCE(SUM(cost),0), COALESCE(SUM(fee),0) "
           "FROM windows WHERE winner IS NOT NULL")[0]
    open_ = q("SELECT COUNT(*), COALESCE(SUM(cost + fee),0) FROM windows WHERE winner IS NULL")[0]
    first = q("SELECT MIN(ts) FROM events WHERE kind = 'start'")[0][0]
    sig = dict(q("SELECT action, COUNT(*) FROM signals GROUP BY action"))
    won = q("SELECT COALESCE(SUM(CASE WHEN (t.side = w.winner) THEN t.shares ELSE 0 END), 0), COALESCE(SUM(t.shares), 0) "
            "FROM trades t JOIN windows w ON w.slug = t.slug WHERE w.winner IS NOT NULL")[0]
    print(f"running since   {time.strftime('%Y-%m-%d %H:%M', time.gmtime(first)) if first else '-'} UTC")
    print(f"cash            ${cash:,.2f}   (start ${C.BANKROLL_USD:,.0f}; open positions ${open_[1]:,.2f} in {open_[0]} windows)")
    print(f"settled         {sw[0]} windows, PnL ${sw[1]:+,.2f} (net of ${sw[4]:,.2f} fees), {sw[2]} windows won")
    if won[1]:
        print(f"shares won      {100 * won[0] / won[1]:.1f}% of {won[1]:,.0f} settled shares")
    print(f"trades          {n} fills, {sh:,.0f} shares, ${cost:,.2f} + ${fee:,.2f} fees; avg price {cost / sh if sh else 0:.3f}")
    print(f"signals         {sig}")
    if sw[0]:
        print(f"PnL per share   {100 * sw[1] / max(1e-9, q('SELECT SUM(up_sh + dn_sh) FROM windows WHERE winner IS NOT NULL')[0][0]):+.2f} c")
    print("last trades:")
    for r in q("SELECT datetime(ts, 'unixepoch'), slug, side, round(move_bps, 2), ask_signal, round(shares, 1), "
               "round(avg_px, 3) FROM trades ORDER BY id DESC LIMIT 10"):
        print("  ", *r)


if __name__ == "__main__":
    main()

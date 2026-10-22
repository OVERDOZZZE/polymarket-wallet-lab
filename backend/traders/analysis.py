"""Strategy analysis of one wallet on one asset's N-minute Up/Down windows.

PnL is reconstructed from cash flows + the market's final resolution, so it does not
depend on redeem clicks. Fees / maker rebates are NOT included. Reference prices are
Binance 1s klines; Polymarket resolves on Chainlink, so expect basis noise near the strike.
Everything labelled "heuristic" is a statistical hint, not proof.
"""
import json
import math
import time

import numpy as np
import pandas as pd

from ..polymarket.markets import market_map
from ..util import read_json, write_df_atomic, write_text_atomic


def _f(x):
    """JSON-safe float."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(x) else x


def _norm_cdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(np.asarray(x, dtype=float) / math.sqrt(2)))


def _roi(g):
    u = g["usdcSize"].sum()
    return g["payoff"].sum() / u if u > 0 else np.nan


# --------------------------------------------------------------------------
def prepare(df, asset, window_minutes):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["type"] = df["type"].astype(str).str.upper()
    df["side"] = df["side"].astype(str).str.upper()
    for c in ["price", "size", "usdcSize", "timestamp"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["o"] = df["outcome"].astype(str).str.lower()
    pm = market_map(df, asset, window_minutes)
    df = df[df["slug"].isin(pm)].copy()
    if df.empty:
        return df
    df["win_start"] = df["slug"].map(lambda s: pm[s][0])
    df["win_len"] = df["slug"].map(lambda s: pm[s][1])
    df["t_in"] = df["timestamp"] - df["win_start"]
    return df.sort_values("timestamp").reset_index(drop=True)


def window_table(df, res):
    rows = []
    for slug, g in df.groupby("slug"):
        b = g[(g.type == "TRADE") & (g.side == "BUY")]
        s = g[(g.type == "TRADE") & (g.side == "SELL")]
        merged = g.loc[g.type == "MERGE", "size"].sum()
        split = g.loc[g.type == "SPLIT", "size"].sum()
        redeem = g.loc[g.type == "REDEEM", "usdcSize"].sum()
        d = {"slug": slug, "win_start": g.win_start.iloc[0], "n_buys": len(b), "n_sells": len(s)}
        for o in ("up", "down"):
            bo, so = b[b.o == o], s[s.o == o]
            d[f"{o}_sh"] = bo["size"].sum()
            d[f"{o}_cost"] = bo["usdcSize"].sum()
            d[f"{o}_sold_sh"] = so["size"].sum()
            d[f"{o}_sold_usd"] = so["usdcSize"].sum()
        d["merged"], d["split"], d["redeem_usd"] = merged, split, redeem
        net_up = d["up_sh"] - d["up_sold_sh"] - merged + split
        net_dn = d["down_sh"] - d["down_sold_sh"] - merged + split
        d["net_up"], d["net_down"] = net_up, net_dn
        winner = (res.get(slug) or {}).get("winner")
        d["winner"] = winner
        cost = d["up_cost"] + d["down_cost"] + split
        d["cost"] = cost
        if winner in ("up", "down"):
            payout = max(net_up if winner == "up" else net_dn, 0)
            d["payout"] = payout
            d["pnl"] = d["up_sold_usd"] + d["down_sold_usd"] + merged + payout - cost
        else:
            d["payout"], d["pnl"] = np.nan, np.nan
        if d["up_sh"] > 0 and d["down_sh"] > 0:
            d["avg_up"] = d["up_cost"] / d["up_sh"]
            d["avg_down"] = d["down_cost"] / d["down_sh"]
            d["pair_cost"] = d["avg_up"] + d["avg_down"]
            m = min(d["up_sh"], d["down_sh"])
            d["matched_sh"] = m
            d["locked_profit"] = m * (1 - d["pair_cost"])
            d["residual_sh"] = abs(d["up_sh"] - d["down_sh"])
        else:
            d["avg_up"] = d["up_cost"] / d["up_sh"] if d["up_sh"] else np.nan
            d["avg_down"] = d["down_cost"] / d["down_sh"] if d["down_sh"] else np.nan
            d["pair_cost"] = d["locked_profit"] = d["matched_sh"] = np.nan
            d["residual_sh"] = d["up_sh"] + d["down_sh"]
        d["two_sided"] = bool(d["up_sh"] > 0 and d["down_sh"] > 0)
        rows.append(d)
    w = pd.DataFrame(rows).sort_values("win_start").reset_index(drop=True)
    w["t"] = pd.to_datetime(w["win_start"], unit="s", utc=True)
    return w


def enrich_trades(df, res, klines):
    b = df[(df.type == "TRADE") & (df.side == "BUY")].copy()
    b["winner"] = b["slug"].map(lambda s: (res.get(s) or {}).get("winner"))
    b = b[b.winner.isin(["up", "down"])].copy()
    b["won"] = (b["o"] == b["winner"]).astype(float)
    b["payoff"] = b["size"] * b["won"] - b["usdcSize"]
    if klines is not None and len(klines) > 1000 and len(b):
        t, c = klines["t"].values, klines["close"].values.astype(float)
        lr = np.diff(np.log(c), prepend=np.log(c[0]))
        sig = pd.Series(lr).rolling(900, min_periods=300).std().values  # per-second vol

        def idx(ts):
            i = np.searchsorted(t, ts, side="right") - 1
            ic = np.clip(i, 0, len(t) - 1)
            ok = (i >= 0) & (ts - t[ic] <= 5)
            return ic, ok

        ts = b["timestamp"].values
        i_now, ok1 = idx(ts)
        i_str, ok2 = idx(b["win_start"].values)
        i_m5, ok3 = idx(ts - 5)
        ok = ok1 & ok2 & ok3
        px, strike, px5 = c[i_now], c[i_str], c[i_m5]
        with np.errstate(divide="ignore", invalid="ignore"):
            b["btc_delta_bps"] = np.where(ok, np.log(px / strike) * 1e4, np.nan)
            b["btc_mom5_bps"] = np.where(ok, np.log(px / px5) * 1e4, np.nan)
            rem = np.maximum(b["win_len"].values - b["t_in"].values, 1)
            z = np.log(px / strike) / (sig[i_now] * np.sqrt(rem))
        p_up = _norm_cdf(np.where(np.isfinite(z), z, 0.0))
        b["fair_up"] = np.where(ok & np.isfinite(z), p_up, np.nan)
        b["fair"] = np.where(b["o"] == "up", b["fair_up"], 1 - b["fair_up"])
        b["edge"] = b["fair"] - b["price"]
        b["side_sign"] = np.where(b["o"] == "up", 1, -1)
    return b


def bucket_table(b, col, bins, labels=None):
    b = b.copy()
    b["bin"] = pd.cut(b[col], bins, labels=labels, right=False, include_lowest=True)
    rows = []
    for k, g in b.groupby("bin", observed=True):
        sh = g["size"].sum()
        rows.append({"bin": str(k), "trades": len(g), "usd": g.usdcSize.sum(),
                     "avg_px": g.usdcSize.sum() / sh if sh else np.nan,
                     "win_rate": np.average(g.won, weights=g["size"]) if sh else np.nan,
                     "pnl": g.payoff.sum(), "roi": _roi(g)})
    t = pd.DataFrame(rows, columns=["bin", "trades", "usd", "avg_px", "win_rate", "pnl", "roi"])
    t["edge"] = t["win_rate"] - t["avg_px"]
    return t


def timing_bins(L):
    if L == 300:
        edges = [0, 10, 30, 60, 120, 180, 240, 270, 290, 301]
    else:
        edges = sorted({0, 10, 30, 60, *(int(L * f) for f in (0.2, 0.4, 0.6, 0.8)), L - 30, L - 10, L + 1})
    labels = [f"{a}-{b - 1 if b == L + 1 else b}s" for a, b in zip(edges[:-1], edges[1:])]
    return edges, labels


# --------------------------------------------------------------------------
class Analysis:
    def __init__(self):
        self.summary = {}
        self.tables = {}
        self.windows = pd.DataFrame()
        self.buys = pd.DataFrame()
        self.report = ""


def run(activity, res, klines, asset="BTC", window_minutes=5, label=""):
    A = Analysis()
    S = A.summary
    S.update({"asset": asset, "window_minutes": window_minutes, "analyzed_at": time.time(),
              "warnings": [], "hypotheses": []})
    L = window_minutes * 60
    rep = []

    def R(t=""):
        rep.append(t)

    def T(df):
        rep.append("```\n" + df.to_string(index=False, float_format=lambda x: f"{x:.3f}") + "\n```")

    S["activity_rows_total"] = int(len(activity))
    df = prepare(activity, asset, window_minutes)
    S["rows"] = int(len(df))
    R(f"# Strategy report{' - ' + label if label else ''}")
    R(f"\n{asset} {window_minutes}-minute Up/Down windows.")
    if df.empty:
        S["empty"] = True
        R(f"\nNo {asset} {window_minutes}m trades found in the stored activity "
          f"({len(activity):,} rows across all markets).")
        A.report = "\n".join(rep)
        return A
    S["empty"] = False

    w = window_table(df, res)
    b = enrich_trades(df, res, klines)
    done = w.dropna(subset=["pnl"])
    span_days = (df.timestamp.max() - df.timestamp.min()) / 86400
    S.update({"first_ts": int(df.timestamp.min()), "last_ts": int(df.timestamp.max()),
              "windows": int(len(w)), "resolved": int(len(done)), "unresolved": int(len(w) - len(done)),
              "span_days": _f(span_days), "buys": int(len(b)),
              "trades": int((df.type == "TRADE").sum())})
    R(f"\nrows: {len(df):,} | windows traded: {len(w):,} | resolved: {len(done):,} | "
      f"span: {span_days:.1f} days | buys analysed: {len(b):,}")
    if len(w) > len(done):
        S["warnings"].append(f"{len(w) - len(done)} windows have no resolution yet (still open or "
                             "lookup failed) - they are excluded from PnL.")

    chk = done[done.redeem_usd > 0]
    if len(chk) and chk.redeem_usd.sum() > 0:
        S["sanity_ratio"] = _f(chk.payout.sum() / chk.redeem_usd.sum())
        R(f"\nSanity: reconstructed payout / actual redeem cash = {S['sanity_ratio']:.3f} "
          "(about 1.0 means the history is complete)")

    # ---------------- 1. PnL
    R("\n## 1. PnL profile")
    tot = done.pnl.sum()
    dep = done.cost.sum()
    S.update({"total_pnl": _f(tot), "deployed": _f(dep), "roi": _f(tot / dep) if dep else None,
              "win_rate_windows": _f(np.mean(done.pnl > 0)) if len(done) else None})
    daily = pd.DataFrame(columns=["date", "pnl", "deployed", "windows"])
    if len(done):
        R(f"total pnl ${tot:,.0f} on ${dep:,.0f} deployed -> "
          f"{(tot / dep * 100) if dep else float('nan'):.2f}% per $ deployed | "
          f"windows won {np.mean(done.pnl > 0) * 100:.1f}%")
        srt = done.sort_values("pnl", ascending=False)
        top1 = srt.head(max(1, len(srt) // 100)).pnl.sum()
        S["top1pct_pnl"] = _f(top1)
        S["top1pct_share"] = _f(top1 / tot) if tot else None
        S["pnl_ex_top10"] = _f(srt.iloc[10:].pnl.sum())
        R(f"top 1% of windows = ${top1:,.0f} "
          f"({(top1 / tot * 100) if tot else float('nan'):.0f}% of pnl) | "
          f"pnl without best 10 windows = ${S['pnl_ex_top10']:,.0f}")
        cum = done.pnl.cumsum()
        S["max_drawdown"] = _f((cum - cum.cummax()).min())
        dg = done.groupby(done.t.dt.date)
        daily = pd.DataFrame({"pnl": dg.pnl.sum(), "deployed": dg.cost.sum(), "windows": dg.size()})
        daily.index.name = "date"
        daily = daily.reset_index()
        dm, ds = daily.pnl.mean(), daily.pnl.std()
        sharpe = dm / ds * math.sqrt(365) if len(daily) > 3 and ds > 0 else np.nan
        S.update({"daily_mean": _f(dm), "daily_std": _f(ds), "sharpe": _f(sharpe),
                  "days_positive": _f(np.mean(daily.pnl > 0))})
        R(f"max drawdown ${S['max_drawdown']:,.0f} | daily pnl mean ${dm:,.0f}, std ${ds if ds == ds else 0:,.0f}, "
          f"annualised Sharpe {sharpe:.1f}")
        chunks = done.pnl.groupby(np.arange(len(done)) // 1000).sum().values[:15]
        R("rolling check - pnl per 1000 windows: " + ", ".join(f"{x:,.0f}" for x in chunks))
    else:
        R("no resolved windows yet.")
    A.tables["daily"] = daily

    # ---------------- 2. structure
    R("\n## 2. Position structure per window")
    ts = w[w.two_sided]
    both = ts.dropna(subset=["pnl"])
    S["two_sided_n"] = int(len(ts))
    S["two_sided_share"] = _f(len(ts) / len(w))
    R(f"windows with both sides bought: {len(ts)}/{len(w)} ({len(ts) / len(w) * 100:.0f}%)")
    if len(ts):
        S["pair_cost_median"] = _f(ts.pair_cost.median())
        S["pair_below1_share"] = _f(np.mean(ts.pair_cost < 1))
        R(f"pair cost (avg Up + avg Down): median {ts.pair_cost.median():.3f}, "
          f"share < 1.00: {np.mean(ts.pair_cost < 1) * 100:.0f}%")
        if len(both):
            denom = both.up_sh.sum() + both.down_sh.sum()
            S["hedge_ratio"] = _f(2 * both.matched_sh.sum() / denom) if denom else None
            S["locked_profit"] = _f(both.locked_profit.sum())
            S["two_sided_pnl"] = _f(both.pnl.sum())
            S["directional_pnl"] = _f(both.pnl.sum() - both.locked_profit.sum())
            R(f"matched/total shares: {S['hedge_ratio'] or float('nan'):.2f} (1.0 = fully hedged) | "
              f"locked-in profit ${S['locked_profit']:,.0f} vs actual pnl ${S['two_sided_pnl']:,.0f} "
              f"-> directional part ${S['directional_pnl']:,.0f}")
    S.update({"buys_per_window_median": _f(w.n_buys.median()), "buys_per_window_p90": _f(w.n_buys.quantile(.9)),
              "sell_window_share": _f(np.mean(w.n_sells > 0)), "merge_window_share": _f(np.mean(w.merged > 0))})
    R(f"buys per window: median {w.n_buys.median():.0f}, p90 {w.n_buys.quantile(.9):.0f}, "
      f"sells used in {np.mean(w.n_sells > 0) * 100:.0f}% of windows, merges in {np.mean(w.merged > 0) * 100:.0f}%")
    span_windows = max(1.0, (df.timestamp.max() - df.timestamp.min()) / L)
    S["participation"] = _f(min(1.0, len(w) / span_windows))
    R(f"share of windows traded out of all windows in span: {S['participation'] * 100:.0f}%")

    # ---------------- 3. timing
    R("\n## 3. When in the window does it trade, and where is the money made?")
    edges, labels = timing_bins(L)
    if len(b):
        tb = bucket_table(b, "t_in", edges, labels)
        T(tb)
    else:
        tb = pd.DataFrame()
        R("no resolved buys.")
    A.tables["timing"] = tb
    first = df[(df.type == "TRADE") & (df.side == "BUY")].groupby("slug").t_in.min()
    if len(first):
        S.update({"first_buy_median": _f(first.median()), "first_buy_std": _f(first.std()),
                  "first_buy_p10": _f(first.quantile(.1)), "first_buy_p90": _f(first.quantile(.9))})
        R(f"first buy offset: median {first.median():.0f}s, std {first.std():.1f}s, "
          f"p10 {first.quantile(.1):.0f}s, p90 {first.quantile(.9):.0f}s (a tight spread = scheduled bot)")

    # ---------------- 4. price buckets
    R("\n## 4. Entry price vs outcome (calibration). edge = win_rate - avg_price")
    if len(b):
        pb = bucket_table(b, "price", [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .98, 1.01])
        T(pb)
    else:
        pb = pd.DataFrame()
    A.tables["price"] = pb

    # ---------------- 5. automation fingerprints
    R("\n## 5. Automation fingerprints")
    bt = df[df.type == "TRADE"].sort_values("timestamp")
    gaps = bt.timestamp.diff().dropna()
    hours = pd.to_datetime(bt.timestamp, unit="s", utc=True).dt.hour
    hourly = pd.DataFrame({"hour": range(24)})
    hourly["trades"] = hourly.hour.map(hours.value_counts()).fillna(0).astype(int)
    if len(b):
        bh = b.assign(hour=pd.to_datetime(b.timestamp, unit="s", utc=True).dt.hour).groupby("hour")
        hourly["usd"] = hourly.hour.map(bh.usdcSize.sum()).fillna(0)
        hourly["pnl"] = hourly.hour.map(bh.payoff.sum()).fillna(0)
    A.tables["hourly"] = hourly
    if len(gaps):
        S.update({"same_second_share": _f(np.mean(gaps == 0)), "median_gap_s": _f(gaps.median()),
                  "longest_silence_h": _f(gaps.max() / 3600)})
        R(f"gap between trades: same-second {np.mean(gaps == 0) * 100:.0f}%, median {gaps.median():.0f}s")
    S["active_hours"] = int((hourly.trades > 0).sum())
    if len(b):
        sizes = b["size"].round(1)
        top = sizes.value_counts().head(6)
        S["top_sizes"] = [[_f(k), _f(v / len(sizes))] for k, v in top.items()]
        R("most common sizes: " + ", ".join(f"{k:g} ({v / len(sizes) * 100:.0f}%)" for k, v in top.items()))
    R(f"active in {S['active_hours']}/24 UTC hours; longest silence {S.get('longest_silence_h') or 0:.1f}h "
      "(24/7 with no sleep gap = automated)")
    multi_share = np.nan
    if len(b):
        cl = (b.sort_values("timestamp")
                .assign(grp=lambda x: ((x.slug != x.slug.shift()) | (x.o != x.o.shift()) |
                                       (x.price.round(2) != x.price.round(2).shift()) |
                                       (x.timestamp.diff() > 3)).cumsum()))
        cs = cl.groupby("grp").agg(n=("size", "size"), tot=("size", "sum"))
        multi_share = np.mean(cs.n >= 2)
        S["fill_clusters"] = int(len(cs))
        S["multi_fill_share"] = _f(multi_share)
        R(f"fill clusters (same market/side/price within 3s): {len(cs)}; with >=2 fills: "
          f"{multi_share * 100:.0f}%  [heuristic: many odd-sized partial fills at one price = "
          "resting limit orders, i.e. maker behaviour]")

    # ---------------- 6. signals vs reference price
    sc = {}
    A.tables["edge_deciles"] = pd.DataFrame()
    if "edge" in b.columns and b["edge"].notna().sum() > 200:
        bb = b.dropna(subset=["edge", "btc_delta_bps"]).copy()
        R(f"\n## 6. Signal tests vs {asset} reference price (rows with price data: {len(bb):,})")
        bb["with_trend"] = np.sign(bb.btc_delta_bps) == bb.side_sign
        bb["with_mom5"] = np.sign(bb.btc_mom5_bps) == bb.side_sign
        big = bb[bb.btc_mom5_bps.abs() > 2]
        sc = {"n": int(len(bb)), "aligned": _f(bb.with_trend.mean()),
              "mom": _f(big.with_mom5.mean()) if len(big) else None, "mom_n": int(len(big)),
              "mean_edge": _f(bb.edge.mean()), "edge_pos": _f(np.mean(bb.edge > 0))}
        R(f"buys aligned with the current price-vs-strike direction (buying the favourite): "
          f"{bb.with_trend.mean() * 100:.0f}%")
        R(f"after a >2bps move in the previous 5s, buys in the direction of that move: "
          f"{(big.with_mom5.mean() * 100) if len(big) else float('nan'):.0f}% (n={len(big)}) "
          "[~50% = no latency/momentum reaction]")
        R(f"mean model edge (fair prob - price) on its buys: {bb.edge.mean():+.3f}; "
          f"buys with edge>0: {np.mean(bb.edge > 0) * 100:.0f}%")
        try:
            bb["q"] = pd.qcut(bb.edge, 10, duplicates="drop")
            de = bb.groupby("q", observed=True).agg(
                edge_mid=("edge", "median"), trades=("edge", "size"),
                usd=("usdcSize", "sum"), payoff=("payoff", "sum"))
            de["roi"] = de.payoff / de.usd
            de.index = de.index.astype(str)
            de = de.reset_index().rename(columns={"q": "edge_bin"})
            R("ROI by model-edge decile (rising ROI = a probability-model edge exists):")
            T(de)
            # Spearman = Pearson on ranks (avoids a scipy dependency)
            sc["edge_spearman"] = _f(pd.Series(np.arange(len(de)), dtype=float).corr(
                de["roi"].rank().reset_index(drop=True)))
            A.tables["edge_deciles"] = de
        except Exception as e:
            R(f"(edge deciles skipped: {e})")
    else:
        R("\n## 6. Signal tests skipped (no reference-price data, or too few trades inside its range)")
        S["warnings"].append("Signal tests skipped: no reference price data for this range.")
    S["signals"] = sc

    # ---------------- 7. scorecard
    R("\n## 7. Hypothesis scorecard (heuristic)")
    hyp = []
    late = b[(b.t_in >= L - 30) & (b.price >= .9)] if len(b) else b
    cheap = b[b.price <= .1] if len(b) else b

    def sleeve(g):
        return {"buys": int(len(g)), "usd": _f(g.usdcSize.sum()) if len(g) else 0.0,
                "roi": _f(_roi(g)) if len(g) else None, "pnl": _f(g.payoff.sum()) if len(g) else 0.0}

    if len(ts):
        lp_share = (S.get("locked_profit") or 0) / tot if tot else np.nan
        hyp.append({"id": "H1", "name": "Two-sided complete-set arb",
                    "text": f"{len(ts) / len(w) * 100:.0f}% of windows two-sided, "
                            f"{S.get('pair_below1_share', 0) * 100:.0f}% of those with pair cost < 1. "
                            f"Locked profit is {lp_share * 100:.0f}% of total pnl."})
    s2 = sleeve(late)
    hyp.append({"id": "H2", "name": f"Late favourite sniping (t>={L - 30}s, price>=0.90)", **s2,
                "text": f"{s2['buys']} buys, ${s2['usd']:,.0f}, ROI "
                        f"{(s2['roi'] or float('nan')) * 100:.2f}%, pnl ${s2['pnl']:,.0f}."})
    s3 = sleeve(cheap)
    hyp.append({"id": "H3", "name": "Cheap lottery tickets (price<=0.10)", **s3,
                "text": f"{s3['buys']} buys, ${s3['usd']:,.0f}, ROI "
                        f"{(s3['roi'] or float('nan')) * 100:.1f}%, pnl ${s3['pnl']:,.0f}."})
    if sc.get("edge_spearman") is not None:
        hyp.append({"id": "H4", "name": "Volatility / probability model",
                    "text": f"edge-vs-ROI Spearman {sc['edge_spearman']:+.2f}, "
                            f"{sc['edge_pos'] * 100:.0f}% of buys have model edge>0."})
    if sc:
        hyp.append({"id": "H5", "name": "Latency / momentum (Chainlink lag)",
                    "text": f"{(sc['mom'] or float('nan')) * 100:.0f}% aligned after 5s jumps; "
                            f"{sc['aligned'] * 100:.0f}% of buys on the currently-winning side."})
    if multi_share == multi_share:
        hyp.append({"id": "H6", "name": "Passive market making",
                    "text": f"{multi_share * 100:.0f}% of fill clusters have multiple fills."})
    S["hypotheses"] = hyp
    for h in hyp:
        R(f"- **{h['id']} {h['name']}:** {h['text']}")
    R("\nHow to read it: compare pnl by sleeve. The sleeve (H1-H6) whose pnl is large AND has a stable "
      "positive ROI in sections 3/4 is the engine; the others are noise or hedges.")

    A.windows, A.buys, A.report = w, b, "\n".join(rep)
    return A


# --------------------------------------------------------------------------
BUYS_ROW_GROUP = 20000     # buys.parquet is sorted by slug; small groups let one window be read alone
BUY_COLS = ["timestamp", "slug", "o", "price", "size", "usdcSize", "t_in", "winner", "won", "payoff",
            "btc_delta_bps", "btc_mom5_bps", "fair", "edge", "transactionHash"]


def save(A, out_dir):
    """summary.json, report.md, small tables as CSV, windows.csv, and buys.parquet (large; read per window)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("*.csv")) + list(out_dir.glob("*.parquet")):   # drop a previous run's outputs
        try:
            old.unlink()
        except OSError:
            pass
    for name, t in A.tables.items():
        write_df_atomic(t, out_dir / f"{name}.csv")
    write_df_atomic(A.windows, out_dir / "windows.csv")
    if len(A.buys):
        b = A.buys[[c for c in BUY_COLS if c in A.buys.columns]].copy()
        for c in ("slug", "o", "winner", "transactionHash"):
            if c in b:
                b[c] = b[c].astype("string")
        b = b.sort_values(["slug", "timestamp"], kind="stable")
        write_df_atomic(b, out_dir / "buys.parquet", row_group_size=BUYS_ROW_GROUP)
    write_text_atomic(out_dir / "report.md", A.report)
    write_text_atomic(out_dir / "summary.json", json.dumps(A.summary, indent=1, default=str))


def load_summary(out_dir):
    return read_json(out_dir / "summary.json", None)

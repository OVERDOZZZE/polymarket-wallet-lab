// Profile: our trading wallet's public Polymarket profile - picture, name, bio, all-time stats and rank, P/L over
// time with where it came from (trading, fees, rebates), positions, closed positions and the activity history.
import { Fragment, ReactNode, useEffect, useMemo, useState } from "react";
import { C, LineChart } from "../../components/charts";
import { Alert, Chip, Empty, Seg, Table, Tabs } from "../../components/ui";
import { api } from "../../lib/api";
import { cents, money, num, pct, shortWallet, signed, tone } from "../../lib/format";
import { useMedia, usePoll } from "../../lib/hooks";

const RANGES = { "1D": "1d", "1W": "1w", "1M": "1m", ALL: "all" } as const;
const RANGE_SUB: Record<string, string> = { "1D": "Past day", "1W": "Past week", "1M": "Past month", ALL: "All time" };
const TABS = ["Positions", "Closed", "Activity"] as const;
const when = (ts: number) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const int = (x: number | null | undefined) => (x === null || x === undefined ? "–" : Math.round(x).toLocaleString("en-US"));

export default function ProfilePage() {
  const { data: p, error } = usePoll<any>("/api/profile", 30000);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Positions");
  if (error && !p) return <div className="page"><Empty title="Profile unavailable">{error}</Empty></div>;
  if (!p) return <div className="page"><p className="hint">Loading your Polymarket profile…</p></div>;
  const a = p.all_time || {}, r = p.rank || {};
  const url = p.name ? `https://polymarket.com/@${p.name}` : `https://polymarket.com/profile/${p.wallet}`;
  const rebates = (a.maker_rebate || 0) + (a.taker_rebate || 0);
  return (
    <div className="page">
      <div className="card prof-hero">
        {p.image ? <img className="prof-ava" src={p.image} alt="" /> : <div className="pm-ava b prof-ava" />}
        <div className="prof-id">
          <div className="prof-name">{p.name || shortWallet(p.wallet)}{p.verified && <span className="prof-check" title="Verified">✓</span>}</div>
          {p.pseudonym && p.pseudonym !== p.name && <div className="muted small">{p.pseudonym}</div>}
          {p.bio && <p className="prof-bio">{p.bio}</p>}
          <div className="row wrap" style={{ gap: 6 }}>
            {p.join_date && <Chip>Joined {new Date(p.join_date * 1000).toLocaleDateString([], { month: "short", year: "numeric" })}</Chip>}
            {p.views !== null && p.views !== undefined && <Chip>{int(p.views)} views</Chip>}
            {p.taker_tier && <Chip dot="var(--btc)">{p.taker_tier} taker tier</Chip>}
            {p.x_username && <Chip href={`https://x.com/${p.x_username}`}>@{p.x_username}</Chip>}
            <Chip href={`https://polygonscan.com/address/${p.wallet}`} mono>{shortWallet(p.wallet)}</Chip>
          </div>
        </div>
        <span className="spacer" />
        <a className="btn primary" href={url} target="_blank" rel="noreferrer">Open on Polymarket</a>
      </div>
      {p.errors?.length > 0 && <div style={{ margin: "12px 0" }}><Alert kind="warn">Some profile data didn't load: {p.errors.join(" · ")}</Alert></div>}

      <div className="bt-grid" style={{ marginTop: 16 }}>
        <div className={`bt-card bt-hero ${tone(a.economic_pnl)}`} style={{ "--i": 0 } as any} title="Positions P/L plus rebates and other wallet income, all time (Polymarket's economic P/L)">
          <div className="bt-l">All-time profit / loss</div>
          <div className="bt-hero-row">
            <span className={`bt-v big num ${tone(a.economic_pnl)}`}>{signed(a.economic_pnl)}</span>
            {r.rank_pnl && <span className="bt-chip">#{int(r.rank_pnl)}</span>}
          </div>
          <div className="bt-s">trading {signed(a.trade_pnl)} · fees {money(a.fees_paid, 2)} · rebates <span className="up">+{money(rebates, 2)}</span></div>
        </div>
        <Tile i={1} l="Cash" v={p.cash === null || p.cash === undefined ? "–" : money(p.cash, 2)} s="USDC in the wallet" />
        <Tile i={2} l="Volume" v={money(a.volume)} s={r.rank_volume ? `rank #${int(r.rank_volume)}` : "all time"} />
        <Tile i={3} l="Markets traded" v={int(p.markets_traded)} s={`${int(a.trade_count)} trades`} />
        <Tile i={4} l="Biggest win" v={money(p.biggest_win, 2)} s="one market" tone="up" />
        <Tile i={5} l="Rebates" v={money(rebates, 2)} s={`maker ${money(a.maker_rebate, 2)} · taker ${money(a.taker_rebate, 2)}`} />
      </div>

      <PnlSection />

      <Tabs value={tab} options={TABS} onChange={setTab} />
      {tab === "Positions" && <Positions />}
      {tab === "Closed" && <Closed />}
      {tab === "Activity" && <Activity />}
    </div>
  );
}

const Tile = ({ i, l, v, s, tone: t }: { i: number; l: string; v: ReactNode; s: ReactNode; tone?: string }) => (
  <div className="bt-card" style={{ "--i": i } as any}>
    <div className="bt-l">{l}</div>
    <div className={`bt-v num ${t || ""}`}>{v}</div>
    <div className="bt-s">{s}</div>
  </div>
);

// P/L over the chosen range + what moved it (each user-pnl point is cumulative, so a range = last - first)
function PnlSection() {
  const [rng, setRng] = useState<keyof typeof RANGES>("1W");
  const { data, error } = usePoll<any>(`/api/profile/pnl?interval=${RANGES[rng]}`, 60000, [rng]);
  const { x, y, change } = useMemo(() => {
    const pts: [number, number | null][] = data?.points || [];
    const base = pts.find((q) => q[1] !== null)?.[1] ?? 0;
    return { x: pts.map((q) => q[0]), y: pts.map((q) => (q[1] === null ? null : q[1] - base)), change: pts.length ? (pts[pts.length - 1][1] ?? base) - base : null };
  }, [data]);
  const f = data?.first || {}, l = data?.last || {};
  const d = (k: string) => (l[k] ?? 0) - (f[k] ?? 0);
  const rows: [string, ReactNode, string?][] = [
    ["Trading (positions)", signed(d("trade_pnl")), tone(d("trade_pnl"))],
    ["Fees paid", money(d("fees_paid"), 2)],
    ["Maker rebates", `+${money(d("maker_rebate"), 2)}`, "up"],
    ["Taker rebates", `+${money(d("taker_rebate"), 2)}`, "up"],
    ["Rewards", `+${money(d("reward_income"), 2)}`],
    ["Unrealized now", signed(l.unrealized_pnl)],
    ["Volume", money(d("volume"))],
    ["Trades", int(d("trade_count"))],
  ];
  return (
    <div className="grid2 prof-pnl">
      <div className="card">
        <div className="row">
          <span className="t2"><span className={tone(change) || "muted"}>{(change ?? 0) >= 0 ? "▲" : "▼"}</span> Profit/Loss</span>
          <span className="spacer" />
          <Seg value={rng} options={Object.keys(RANGES) as (keyof typeof RANGES)[]} onChange={setRng} />
        </div>
        <div className={`big-value ${tone(change)}`}>{change === null ? "–" : signed(change)}</div>
        <div className="muted small" style={{ marginBottom: 8 }}>{RANGE_SUB[rng]}</div>
        {error && !data ? <p className="hint dn">{error}</p> : (
          <LineChart x={x} ys={[y]} time height={220} zero legend={false}
                     series={[{ label: "P/L", color: C.pnl, width: 2.5, gradient: true, fmt: (v) => signed(v) }]}
                     yFmt={(v) => "$" + v.toFixed(0)} />
        )}
      </div>
      <div className="card">
        <div className="card-title">Where it came from <span className="muted small" style={{ fontWeight: 500 }}>{RANGE_SUB[rng].toLowerCase()}</span></div>
        <div className="pm-rows">
          {rows.map(([k, v, c]) => <Fragment key={k}><span className="l">{k}</span><span className={`v ${c || ""}`}>{v}</span></Fragment>)}
        </div>
        <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>Polymarket's own accounting (Data API user-pnl). Profit/loss = positions + rebates and other wallet income, after fees.</p>
      </div>
    </div>
  );
}

/** Cursor-paged list from one of the /api/profile lists; "Load more" appends the next page. */
function usePaged(url: string) {
  const [rows, setRows] = useState<any[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  async function load(cursor: string | null) {
    setBusy(true);
    try {
      const d = await api.get<any>(cursor ? `${url}${url.includes("?") ? "&" : "?"}cursor=${encodeURIComponent(cursor)}` : url);
      setRows((r) => (cursor ? [...r, ...d.rows] : d.rows));
      setNext(d.next || null);
      setErr(null);
    } catch (e: any) { setErr(e.message || String(e)); }
    setBusy(false);
    setLoaded(true);
  }
  useEffect(() => { setRows([]); setLoaded(false); load(null); }, [url]);  // eslint-disable-line react-hooks/exhaustive-deps
  const more = next ? <div className="row" style={{ justifyContent: "center", padding: 14 }}><button className="btn sm" disabled={busy} onClick={() => load(next)}>{busy ? "Loading…" : "Load more"}</button></div> : null;
  return { rows, loaded, err, more };
}

const Market = ({ r, sub }: { r: any; sub?: ReactNode }) => (
  <a className="mkt" href={r.event_slug ? `https://polymarket.com/event/${r.event_slug}` : undefined} target="_blank" rel="noreferrer" style={{ textDecoration: "none" }}>
    {r.icon ? <img className="mkt-img" src={r.icon} alt="" loading="lazy" /> : <div className="mkt-ico">₿</div>}
    <div style={{ minWidth: 0 }}>
      <div className="mkt-t" title={r.title}>{String(r.title || "–").replace("Bitcoin Up or Down - ", "BTC Up/Down · ")}</div>
      {sub && <div className="mkt-s">{sub}</div>}
    </div>
  </a>
);

/** "BTC 5m · Oct 28 17:55" from an Up/Down slug (local time of the window start); other markets keep their title. */
const shortMarket = (r: any) => {
  const m = /^(\w+)-updown-(\d+)m-(\d+)$/.exec(r.event_slug || r.slug || "");
  return m ? `${m[1].toUpperCase()} ${m[2]}m · ${when(Number(m[3]))}` : String(r.title || "–");
};

/** Phones: one compact row per item (icon · name + detail · amount + sub) instead of a wide table. */
const MRow = ({ r, title, sub, right, rightSub }: { r: any; title: ReactNode; sub: ReactNode; right: ReactNode; rightSub?: ReactNode }) => {
  const body = (
    <>
      {r.icon ? <img className="mkt-img" src={r.icon} alt="" loading="lazy" /> : <div className="mkt-ico">{r.title ? "₿" : "$"}</div>}
      <div className="mrow-main"><div className="mrow-t">{title}</div><div className="mrow-s">{sub}</div></div>
      <div className="mrow-r"><div className="mrow-v">{right}</div>{rightSub && <div className="mrow-s">{rightSub}</div>}</div>
    </>
  );
  return r.event_slug
    ? <a className="mrow" href={`https://polymarket.com/event/${r.event_slug}`} target="_blank" rel="noreferrer">{body}</a>
    : <div className="mrow">{body}</div>;
};
const MList = ({ children, more }: { children: ReactNode; more: ReactNode }) => <div className="card flush mlist">{children}{more}</div>;

const outcome = (o: string) => (o ? <span className={`pill ${String(o).toLowerCase() === "up" || String(o).toLowerCase() === "yes" ? "up" : "dn"}`}>{o}</span> : null);

function Positions() {
  const { rows, loaded, err, more } = usePaged("/api/profile/positions?status=open");
  const phone = useMedia("(max-width: 640px)");
  if (err && !rows.length) return <Alert kind="err">{err}</Alert>;
  if (loaded && !rows.length) return <Empty title="No open positions">Positions show here while a market is open, and until the winning shares are redeemed.</Empty>;
  const state = (r: any) => r.status === "OPEN" ? <span className="live-badge on"><span className="dot" />Open</span>
    : (r.current_value || 0) > 0.005 ? <span className="live-badge warn">Redeem</span> : <span className="live-badge off">Lost</span>;
  if (phone) return (
    <MList more={more}>
      {rows.map((r, i) => <MRow key={i} r={r} title={shortMarket(r)}
        sub={<>{outcome(r.outcome)}{num(r.current_size, 1)} sh · {cents(r.avg_price)} → {cents(r.current_price)}</>}
        right={money(r.current_value, 2)}
        rightSub={<>{r.status === "OPEN" ? "" : (r.current_value || 0) > 0.005 ? "Redeem · " : "Lost · "}<span className={tone(r.total_pnl)}>{signed(r.total_pnl)}</span></>} />)}
    </MList>
  );
  return (
    <div className="card flush">
      <Table rows={rows} cols={[
        { key: "title", label: "Market", align: "l", render: (r: any) => <Market r={r} sub={r.last_event_at ? when(r.last_event_at) : r.end_date} /> },
        { key: "outcome", label: "Outcome", render: (r: any) => outcome(r.outcome) },
        { key: "status", label: "State", render: state, sort: (r: any) => r.status },
        { key: "current_size", label: "Shares", render: (r: any) => num(r.current_size, 2) },
        { key: "avg_price", label: "Avg → now", render: (r: any) => <>{cents(r.avg_price)} <span className="muted">→</span> {cents(r.current_price)}</> },
        { key: "total_cost_usdc", label: "Cost", render: (r: any) => money(r.total_cost_usdc, 2) },
        { key: "current_value", label: "Value", render: (r: any) => money(r.current_value, 2) },
        { key: "total_pnl", label: "P/L", render: (r: any) => <span className={tone(r.total_pnl)}>{signed(r.total_pnl)} <span className="small">({pct((r.percent_pnl ?? 0) / 100, 0, true)})</span></span> },
      ]} footer={more ? <tfoot><tr><td colSpan={8} style={{ padding: 0 }}>{more}</td></tr></tfoot> : undefined} />
    </div>
  );
}

function Closed() {
  const { rows, loaded, err, more } = usePaged("/api/profile/positions?status=closed");
  const phone = useMedia("(max-width: 640px)");
  if (err && !rows.length) return <Alert kind="err">{err}</Alert>;
  if (loaded && !rows.length) return <Empty title="No closed positions yet" />;
  const result = (r: any) => (r.current_price >= 0.99 ? "Won" : r.current_price <= 0.01 ? "Lost" : "Sold");
  if (phone) return (
    <MList more={more}>
      {rows.map((r, i) => <MRow key={i} r={r} title={shortMarket(r)}
        sub={<>{outcome(r.outcome)}{num(r.total_size, 1)} sh @ {cents(r.avg_price)}</>}
        right={<span className={tone(r.realized_pnl)}>{signed(r.realized_pnl)}</span>}
        rightSub={<span className={result(r) === "Won" ? "up" : result(r) === "Lost" ? "dn" : ""}>{result(r)}</span>} />)}
    </MList>
  );
  return (
    <div className="card flush">
      <Table rows={rows} cols={[
        { key: "title", label: "Market", align: "l", render: (r: any) => <Market r={r} sub={r.last_event_at ? `closed ${when(r.last_event_at)}` : r.end_date} /> },
        { key: "outcome", label: "Outcome", render: (r: any) => outcome(r.outcome) },
        { key: "total_size", label: "Shares bought", render: (r: any) => num(r.total_size, 2) },
        { key: "avg_price", label: "Avg price", render: (r: any) => cents(r.avg_price) },
        { key: "current_price", label: "Result", render: (r: any) => r.current_price >= 0.99 ? <span className="up">Won</span> : r.current_price <= 0.01 ? <span className="dn">Lost</span> : <span className="muted">Sold</span> },
        { key: "realized_pnl", label: "P/L", render: (r: any) => <span className={tone(r.realized_pnl)}>{signed(r.realized_pnl)}</span> },
      ]} footer={more ? <tfoot><tr><td colSpan={6} style={{ padding: 0 }}>{more}</td></tr></tfoot> : undefined} />
    </div>
  );
}

const ACT: Record<string, string> = { TRADE: "Trade", REDEEM: "Redeem", MERGE: "Merge", SPLIT: "Split", MAKER_REBATE: "Maker rebate",
                                      TAKER_REBATE: "Taker rebate", REWARD: "Reward", CONVERSION: "Conversion", YIELD: "Yield" };

function Activity() {
  const { rows, loaded, err, more } = usePaged("/api/profile/activity");
  const phone = useMedia("(max-width: 640px)");
  if (err && !rows.length) return <Alert kind="err">{err}</Alert>;
  if (loaded && !rows.length) return <Empty title="No activity yet" />;
  // money in (+) or out (−) of the wallet
  const amount = (r: any) => {
    const v = r.usdc_size || 0;
    if (r.type === "TRADE") return r.side === "BUY" ? <span>−{money(v, 2)}</span> : <span className="up">+{money(v, 2)}</span>;
    if (r.type === "SPLIT") return <span>−{money(v, 2)}</span>;
    return v ? <span className="up">+{money(v, 2)}</span> : <span className="muted">$0.00</span>;
  };
  const kind = (r: any) => (r.type === "TRADE" ? `${r.side === "BUY" ? "Buy" : "Sell"} ${r.outcome || ""}` : ACT[r.type] || r.type);
  if (phone) return (
    <MList more={more}>
      {rows.map((r, i) => <MRow key={i} r={r} title={kind(r)}
        sub={r.title ? shortMarket(r) : when(r.timestamp)}
        right={amount(r)}
        rightSub={r.type === "TRADE" ? `${num(r.size, 1)} sh @ ${cents(r.price)}` : r.title ? when(r.timestamp).split(", ").pop() : undefined} />)}
    </MList>
  );
  return (
    <div className="card flush">
      <Table rows={rows} cols={[
        { key: "timestamp", label: "When", align: "l", render: (r: any) => <span className="muted">{when(r.timestamp)}</span> },
        { key: "type", label: "Type", align: "l", render: (r: any) => <b>{r.type === "TRADE" ? (r.side === "BUY" ? "Buy" : "Sell") : ACT[r.type] || r.type}</b> },
        { key: "title", label: "Market", align: "l", render: (r: any) => r.title ? <Market r={r} /> : <span className="muted">–</span> },
        { key: "outcome", label: "Outcome", render: (r: any) => outcome(r.outcome) || <span className="muted">–</span> },
        { key: "price", label: "Price", render: (r: any) => (r.type === "TRADE" ? cents(r.price) : "–") },
        { key: "size", label: "Shares", render: (r: any) => (r.type === "TRADE" || r.type === "REDEEM" || r.type === "MERGE" ? num(r.size, 2) : "–") },
        { key: "usdc_size", label: "Amount", render: amount },
        { key: "tx", label: "", render: (r: any) => r.transaction_hash ? <a className="muted small" href={`https://polygonscan.com/tx/${r.transaction_hash}`} target="_blank" rel="noreferrer">tx ↗</a> : "" },
      ]} footer={more ? <tfoot><tr><td colSpan={8} style={{ padding: 0 }}>{more}</td></tr></tfoot> : undefined} />
    </div>
  );
}

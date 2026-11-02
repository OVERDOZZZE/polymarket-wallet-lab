// Trade: manual live trading on BTC 5m/15m Up/Down windows, Polymarket layout, lowest-latency data.
import { useMemo, useState } from "react";
import { Card, Ring, Seg } from "../../components/ui";
import { FEEDS, FeedId, TWAP_END_LAG, TWAP_S, bookLat, clLastSec, feeds, projectedTwap, twapAt } from "../../lib/feeds";
import { clock, hm, money, num } from "../../lib/format";
import { useMedia, useNow, usePref } from "../../lib/hooks";
import { tradeSocket } from "../../lib/tradeSocket";
import { AccountPanel } from "./AccountPanel";
import { OrderBook } from "./OrderBook";
import { OrderTicket, SellRequest } from "./OrderTicket";
import { PriceChart, SPAN_OPTIONS } from "./PriceChart";
import { WindowPositions } from "./WindowPositions";
import { pickWindows, useFeedTick, useStrike, useTrade } from "./useTrade";

const med = (a: number[]) => (a.length ? [...a].sort((x, y) => x - y)[Math.floor(a.length / 2)] : null);

export default function TradePage() {
  const { markets, acct } = useTrade();
  // This PC's clock can be a second or more off; windows open and close on exchange time, so the countdown,
  // window switching and "closed" checks all use the NTP-corrected time.
  const offset = acct.status?.clock_offset_ms || 0;
  const now = useNow(250) + offset / 1000;
  useFeedTick(4);
  const [minutes, setMinutes] = usePref<number>("trade.minutes", 5);
  const [which, setWhich] = useState<"current" | "next">("current");
  const [span, setSpan] = usePref<string>("trade.span", "5m");
  const [visible, setVisible] = usePref<Record<FeedId, boolean>>("trade.visible", { chainlink: true, coinbase: true, kraken: true, binance: true });
  const [align, setAlign] = usePref<boolean>("trade.align", true);
  const [outcome, setOutcome] = useState<"up" | "down">("up");
  const [limitPx, setLimitPx] = useState<number | null>(null);
  const [sellReq, setSellReq] = useState<SellRequest | null>(null);
  const phone = useMedia("(max-width: 640px)");

  const { current, next } = pickWindows(markets, minutes, now);
  const m = which === "next" ? next : current;
  const strike = useStrike(m, now);
  const cl = feeds.chainlink.last;
  // It settles on the 60 s TWAP at the close, so "current" is the rolling 60 s TWAP (Polymarket shows the same)
  const lastSec = clLastSec();
  const cur = lastSec !== null ? twapAt(lastSec + TWAP_END_LAG) : null;      // the newest 60 ticks
  const delta = cur !== null && strike.value !== null ? cur - strike.value : null;
  // Last minute: where the final TWAP is heading = its ticks so far + spot for the seconds left. Spot is
  // Coinbase + its usual gap to Chainlink (Chainlink reaches us ~1.4 s late), else the last Chainlink tick.
  const cb = feeds.coinbase;
  const spot = cb.last !== null && cb.basisObs.length >= 10 && now - cb.lastRecv / 1000 < 3 ? cb.last + cb.basis : cl;
  const proj = m && now >= m.end_ts - TWAP_S - 15 && now < m.end_ts ? projectedTwap(m.end_ts, spot) : null;
  const projD = proj !== null && strike.value !== null ? proj - strike.value : null;
  const left = m ? (now < m.start ? m.start - now : m.end_ts - now) : 0;
  const open = !!m && now >= m.start, hot = open && left < 30;
  const myPrices = useMemo(() => new Set(acct.orders.filter((o) => m && o.asset_id === m.tokens[outcome]).map((o) => Math.round(Number(o.price) * 1000) / 1000)), [acct.orders, m, outcome]);
  const st = acct.status;

  return (
    <div className="page">
      <div className="mkt-head" style={{ marginBottom: 16 }}>
        <div className="mkt-ico">₿</div>
        <div>
          <h1>Bitcoin Up or Down · {minutes} min</h1>
          <div className="muted">{m ? `${hm(m.start)} – ${hm(m.end_ts)} · ${m.title}` : "Loading windows…"}</div>
        </div>
        <span className="spacer" />
        <Seg value={minutes} options={[5, 15] as const} onChange={(v) => { setMinutes(v); setLimitPx(null); }} format={(v) => `${v} min`} />
        <Seg value={which} options={["current", "next"] as const} onChange={(v) => { setWhich(v); setLimitPx(null); }} format={(v) => (v === "current" ? "Current" : "Next")} />
      </div>

      <div className="trade-grid">
        <div className="col trade-main" style={{ gap: 16 }}>
          <Card>
            <div className="ptb" style={{ marginBottom: 12 }}>
              <div><div className="l">Price to beat</div><div className="v num">{strike.value !== null ? money(strike.value, 2) : "–"}</div><div className="muted small">{strike.src}</div></div>
              <div><div className="l" title="The window settles on Chainlink's 60-second average price at the close, not on a single tick">Current price · 60 s TWAP</div>
                <div className="v num">{cur !== null ? money(cur, 2) : "–"} {delta !== null && <span className={delta >= 0 ? "up" : "dn"} style={{ fontSize: "1rem" }}>{delta >= 0 ? "▲" : "▼"} {money(Math.abs(delta), 2)}</span>}</div>
                <div className="muted small">{delta === null ? " " : delta >= 0 ? "Up is winning" : "Down is winning"} · spot {money(cl, 2)}
                  {projD !== null && <span title="The last minute's ticks so far plus the current (Coinbase-led) spot for the seconds left"> · close heading to <span className={projD >= 0 ? "up" : "dn"}>{projD >= 0 ? "▲" : "▼"} {money(Math.abs(projD), 2)}</span></span>}
                </div>
              </div>
              <span className="spacer" />
              <div className="cd">
                <Ring frac={!m ? 0 : open ? left / (m.end_ts - m.start) : 1} size={phone ? 80 : 100} stroke={phone ? 7 : 8}
                      color={!open ? "var(--surface-4)" : hot ? "var(--down)" : undefined}>
                  <div className={`countdown${hot ? " hot" : ""}`}>{clock(left)}</div>
                  <div className="cd-l">{m && !open ? "opens in" : "left"}</div>
                </Ring>
              </div>
            </div>
            <div className="row wrap" style={{ marginBottom: 8, gap: 8 }}>
              {FEEDS.map((f) => {
                const fd = feeds[f.id];
                const lat = fd.latency();
                const shown = fd.last !== null ? fd.last + (align && f.id !== "chainlink" ? fd.basis : 0) : null;
                return (
                  <span key={f.id} className={`feed-chip${visible[f.id] ? "" : " off"}`} onClick={() => setVisible({ ...visible, [f.id]: !visible[f.id] })}
                        title={`${f.label}: ${fd.status}. Raw ${num(fd.last, 2)}${f.id !== "chainlink" ? `, basis to Chainlink ${num(fd.basis, 2)}` : ""}. Latency = exchange timestamp to this browser (NTP-corrected).`}>
                    <span className="sw" style={{ background: f.color }} />{f.label}
                    <b>{shown !== null ? num(shown, 2) : "–"}</b>
                    <span className="muted">{lat !== null ? `${Math.round(lat + offset)} ms` : fd.status}</span>
                  </span>
                );
              })}
              <span className="spacer" />
              <label className="check small" title="Shift Coinbase/Kraken/Binance by their median gap to Chainlink so the lines overlay (shows who leads)."><input type="checkbox" checked={align} onChange={(e) => setAlign(e.target.checked)} />Align to Chainlink</label>
              <Seg value={span} options={SPAN_OPTIONS} onChange={setSpan} />
            </div>
            <PriceChart span={span} visible={visible} align={align} strike={strike.value} windowStart={m?.start} windowEnd={m?.end_ts} height={phone ? 230 : 360} />
          </Card>

          <WindowPositions m={m} acct={acct} onSell={(o, sh) => setSellReq({ outcome: o, shares: sh, n: Date.now() })}
                           onAll={() => document.getElementById("account")?.scrollIntoView({ behavior: "smooth", block: "start" })} />

          <div className="grid2">
            <div className="card flush">
              <div className="row" style={{ padding: "16px 18px 8px" }}>
                <div className="card-title" style={{ margin: 0 }}>Order book</div>
                <span className="spacer" />
                <Seg value={outcome} options={["up", "down"] as const} onChange={setOutcome} format={(v) => (v === "up" ? "Up" : "Down")} />
              </div>
              <OrderBook token={m?.tokens[outcome]} outcome={outcome} myPrices={myPrices} onPick={(p) => setLimitPx(p)} />
            </div>
            <Card title="Latency">
              <div className="ticket-rows" style={{ margin: 0 }}>
                <span className="l">Order books (Polymarket → browser)</span><span className="v">{fmtMs(med(bookLat), offset)}</span>
                {FEEDS.map((f) => <FeedLat key={f.id} id={f.id} offset={offset} />)}
                <span className="l">Browser ↔ server</span><span className="v">{tradeSocket.rtt !== null ? `${tradeSocket.rtt.toFixed(1)} ms` : "–"}</span>
                <span className="l">Server ↔ Polymarket CLOB (warm)</span><span className="v">{st?.rtt_ms?.p50 ? `${Math.round(st.rtt_ms.p50)} ms` : "–"}</span>
                <span className="l">Order sign + post (server, median)</span><span className="v">{st?.order_ms?.p50 ? `${Math.round(st.order_ms.p50)} ms` : "no orders yet"}</span>
                <span className="l">Fill stream (user channel)</span><span className="v">{st?.user_ws || "–"}</span>
                <span className="l">PC clock vs NTP</span><span className="v">{st?.clock_offset_ms !== null && st?.clock_offset_ms !== undefined ? `${st.clock_offset_ms.toFixed(0)} ms (corrected)` : "–"}</span>
              </div>
              <p className="hint" style={{ marginTop: 10, marginBottom: 0 }}>Prices stream from each exchange straight into this tab. Orders travel over one open websocket to the server, which signs locally and posts on a pre-warmed connection.</p>
            </Card>
          </div>
          <div id="account"><AccountPanel acct={acct} markets={markets} now={now} /></div>
        </div>
        <div className="trade-ticket">
          <OrderTicket m={m} acct={acct} now={now} outcome={outcome} setOutcome={setOutcome} limitPx={limitPx} setLimitPx={setLimitPx} sellReq={sellReq} />
        </div>
      </div>
    </div>
  );
}

const fmtMs = (v: number | null, off: number) => (v === null ? "–" : `${Math.round(v + off)} ms`);

function FeedLat({ id, offset }: { id: FeedId; offset: number }) {
  const f = feeds[id];
  return <><span className="l">{FEEDS.find((x) => x.id === id)!.label} (exchange → browser)</span><span className="v">{fmtMs(f.latency(), offset)}</span></>;
}

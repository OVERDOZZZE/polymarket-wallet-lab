// Settings form for a bot. Known keys get labels/help; any other config key still gets a plain input,
// so new strategies work without UI changes.
import { useState } from "react";
import { Field, Modal, Switch, Tabs, toast } from "../../components/ui";
import { api } from "../../lib/api";

type Meta = { label: string; help?: string; group: string; step?: number; options?: [string, string][] };
const META: Record<string, Meta> = {
  windows: { label: "Window lengths (min)", help: "Comma-separated, e.g. 5, 15", group: "Quoting" },
  levels: { label: "Levels per side", group: "Quoting", step: 1 },
  sizes: { label: "Shares per level", help: "Top level first. Polymarket's minimum is 5 shares.", group: "Quoting" },
  base_edge: { label: "Edge below fair", help: "Top bid = fair − edge. A filled Up+Down pair costs about 1 − 2 × edge.", group: "Quoting", step: 0.005 },
  level_step: { label: "Level step", group: "Quoting", step: 0.01 },
  model_weight: { label: "Model weight", help: "Fair = book mid + Coinbase lead + weight × (model − mid). 0 trusts the market.", group: "Quoting", step: 0.05 },
  lead_weight: { label: "Coinbase lead weight", help: "How much of the move Coinbase predicts for Chainlink (~1.4 s ahead) to price in.", group: "Quoting", step: 0.1 },
  join_only: { label: "Join the best bid, never improve it", help: "Safer: makers in front absorb informed flow first. Fewer fills.", group: "Quoting" },
  skew_per_100: { label: "Skew per 100 shares", help: "Lower the heavier side's bids by this per 100 shares of imbalance.", group: "Quoting", step: 0.005 },
  start_delay_s: { label: "Start after open (s)", group: "Quoting", step: 1 },
  stop_before_end_s: { label: "Stop before close (s)", group: "Quoting", step: 1 },
  bankroll: { label: "Bankroll (USDC)", help: "Changing it does not reset the account; use New account for that.", group: "Risk", step: 100 },
  max_side_shares: { label: "Max shares per side per window", group: "Risk", step: 50 },
  max_imbalance: { label: "Max imbalance (shares)", help: "Stop bidding the side you hold more of once it leads by this many shares.", group: "Risk", step: 25 },
  max_window_cost: { label: "Max $ per window", help: "Above this only the lighter side keeps bidding, until flat.", group: "Risk", step: 50 },
  min_price: { label: "Lowest buy price", group: "Risk", step: 0.01 },
  max_price: { label: "Highest buy price", group: "Risk", step: 0.01 },
  jump_bps: { label: "Pull quotes on a Coinbase move of (bps in 1 s)", group: "Risk", step: 0.5 },
  jump_cooldown_ms: { label: "Stay out after a jump (ms)", group: "Risk", step: 100 },
  vol_floor: { label: "Volatility floor (bps/√s)", group: "Risk", step: 0.1 },
  spot_source: { label: "Spot feed for the model", help: "coinbase, binance, or blend (both, each adjusted to Chainlink).", group: "Quoting" },
  maker_min_edge: { label: "Min model edge to rest a bid", help: "Bid only at prices at least this far below the pure model's fair value. Below −0.5 = off.", group: "Risk", step: 0.005 },
  taker_fair: { label: "Judge takes on", help: "blend (the quoting fair value) or model (the pure model).", group: "Simulation" },
  latency_ms: { label: "Simulated order latency (ms)", help: "Time for an order or cancel to reach the exchange. The Trade screen measures your real one.", group: "Simulation", step: 10 },
  min_requote_ms: { label: "Min requote gap (ms)", group: "Simulation", step: 5 },
  fee_rate: { label: "Fee rate", group: "Simulation", step: 0.01 },
  rebate_share: { label: "Rebate share of fee", help: "0.15 matches CrouchPredictions' actual payouts; the listed rate is 0.20.", group: "Simulation", step: 0.01 },
  taker_enabled: { label: "Also take when Coinbase shows the ask is stale", group: "Simulation" },
  taker_edge: { label: "Taker edge after fees", group: "Simulation", step: 0.005 },
  taker_size: { label: "Taker size (shares)", group: "Simulation", step: 5 },
  taker_cooldown_ms: { label: "Taker cooldown (ms)", group: "Simulation", step: 100 },
  // machinelearned reconstruction (ml_maker)
  size_pct: { label: "Size, % of their orders", help: "0.5 = half of machinelearned's order sizes (Polymarket minimum 5 shares).", group: "Quoting", step: 0.05 },
  their_sizes: { label: "Their order sizes (shares)", help: "Below / between / above the price edges.", group: "Quoting" },
  their_size_edges: { label: "Price edges for their sizes", group: "Quoting" },
  early_s: { label: "Early phase (s)", help: "In the first seconds it bids one tick above the best bid.", group: "Quoting", step: 1 },
  early_improve: { label: "Early improvement", group: "Quoting", step: 0.01 },
  late_s: { label: "Late phase starts (s)", help: "After this it bids below the market.", group: "Quoting", step: 5 },
  late_offset: { label: "Late bid below the best bid", group: "Quoting", step: 0.01 },
  ticket_usd: { label: "Late ticket size ($, before size %)", help: "Cheap sides late in the window get ceil($ / price) shares.", group: "Quoting", step: 1 },
  ticket_max_price: { label: "Late tickets up to price", group: "Quoting", step: 0.01 },
  // live bid copier (join_copy)
  target_wallet: { label: "Wallet to copy", help: "0x proxy address of the trader whose fills are copied.", group: "Quoting" },
  order_ttl_s: { label: "Copy bid lifetime (s)", help: "An unfilled copy bid is cancelled after this long.", group: "Quoting", step: 5 },
  copy_takers: { label: "Also copy their taker buys (bid 1 tick under the ask)", group: "Quoting" },
  refill_cooldown_s: { label: "Wait after a fill (s)", help: "Stops re-bidding a side instantly while the price runs through it.", group: "Risk", step: 1 },
  // fast player (fast_player)
  signal_feeds: { label: "Spike signal feeds", help: "Binance futures only (fastest feed; replay 2026-11-02: best of the tested signals, few fills), or classic = the average of Coinbase, Kraken and Binance spot. Custom uses the two fields below. Applies to the running bot at once.", group: "Signal",
    options: [["binance_fut", "Binance futures only"], ["classic", "Classic: Coinbase + Kraken + Binance spot"], ["custom", "Custom (exchanges + combine below)"]] },
  sources: { label: "Exchanges for the spike signal", help: "coinbase, kraken, binance. Each exchange's own move is used, so price gaps between venues never look like a spike.", group: "Signal" },
  spike_bps: { label: "Spike size (bps)", help: "BTC move within the lookback that counts as a spike.", group: "Signal", step: 0.5 },
  lookback_ms: { label: "Spike lookback (ms)", group: "Signal", step: 100 },
  combine: { label: "Combine exchanges", help: "mean (average move) or max (largest move, if no exchange disagrees).", group: "Signal" },
  feed_stale_ms: { label: "Exchange counts as live for (ms)", help: "An exchange with no trade for this long is left out of the signal.", group: "Signal", step: 500 },
  fair_mode: { label: "Fair value", help: "anchored = book mid before the spike + the model's predicted change; model = pure model; both = the lower of the two.", group: "Signal" },
  min_edge: { label: "Min edge after fees ($/share)", help: "Buy only while fair value − price − fee is at least this.", group: "Signal", step: 0.005 },
  max_entries_per_window: { label: "Entries per window", group: "Signal", step: 1 },
  cooldown_s: { label: "Gap between entries (s)", group: "Signal", step: 1 },
  stake_frac: { label: "Stake, share of portfolio", help: "Per entry; at least Polymarket's minimum (5 shares, $1).", group: "Risk", step: 0.01 },
  max_stake_usd: { label: "Max $ per entry", group: "Risk", step: 10 },
  max_window_frac: { label: "Max share of portfolio per window", group: "Risk", step: 0.05 },
  max_open_frac: { label: "Max share of portfolio open", group: "Risk", step: 0.05 },
  max_daily_loss_frac: { label: "Daily loss halt (share of bankroll)", group: "Risk", step: 0.05 },
  exit_mode: { label: "Exit", help: "hold = to settlement; scalp = sell into the bid once the book has repriced.", group: "Exit" },
  take_profit: { label: "Sell at a gain of ($/share, after fees)", group: "Exit", step: 0.005 },
  exit_after_s: { label: "Then sell after (s)", help: "…if the loss is no more than the limit below; otherwise hold to settlement.", group: "Exit", step: 1 },
  max_exit_loss: { label: "Max loss to sell at ($/share)", group: "Exit", step: 0.005 },
  // guarded two-sided maker (guard_maker)
  pull_ms: { label: "Keep the threatened side out for (ms)", help: "After the last spike tick, the side the move makes stale stays unquoted this long.", group: "Signal", step: 250 },
  cancel_latency_ms: { label: "Simulated cancel latency (ms)", help: "Time for a cancel to take effect. Measured from Dublin: ~26 ms p50, ~31 ms p90.", group: "Simulation", step: 5 },
  match_wait_ms: { label: "Quiet-market fill wait (ms)", help: "If no book message arrives after the match time, the order fills against the current book after this long.", group: "Simulation", step: 100 },
  // imbalance maker (imb_maker)
  B: { label: "Size base B", help: "Shares per bid = floor(B / (1 − |p − 0.5|)): 60 at 50¢, 75 at 30¢, 100 at 10¢.", group: "Quoting", step: 5 },
  n_levels: { label: "Ladder levels per token", help: "Bids 1¢ apart going away from the market, none below the lowest buy price.", group: "Quoting", step: 1 },
  imb_join: { label: "Join the best bid at queue share ≥", help: "Share = size at the token's best bid / (that + size at its best ask). Below it the top sits back.", group: "Quoting", step: 0.05 },
  back_ticks: { label: "Ticks behind the best when not joining", group: "Quoting", step: 1 },
  imb_low: { label: "Third tier below share (0 = off)", help: "Below this share the top sits the next setting's ticks back instead.", group: "Quoting", step: 0.05 },
  back_low: { label: "Ticks behind in the third tier", group: "Quoting", step: 1 },
  age_join: { label: "Min age of the best price to join (s)", group: "Quoting", step: 0.05 },
  inv_cap: { label: "Net share cap per window", help: "No Up bids while Up − Down shares bought exceeds this; no Down bids below minus this.", group: "Risk", step: 25 },
  spend_cap_usd: { label: "Spend cap per window ($, 0 = off)", help: "A window whose fills cost this much is not quoted any more (live: the real fills; paper twin: its own).", group: "Risk", step: 10 },
  live_merge: { label: "Merge Up + Down pairs into USDC after each window", help: "Frees the cash of matched pairs at once instead of waiting for resolution (gasless, via Polymarket's relayer).", group: "Live" },
  live_dry_run: { label: "Dry run: live order flow without sending (paper bots, read at start)", help: "Decides, places and cancels against the real books with simulated 30 ms acks; nothing reaches Polymarket.", group: "Live" },
  // live mode (bots.mode = 'live')
  live_paused: { label: "Pause live orders (kill switch)", help: "Takes effect at once; the paper twin keeps trading.", group: "Live" },
  live_scale: { label: "Live size = paper size ×", help: "0.2 = a $100 live account next to a $500 paper one. Orders below $1 are raised to $1.", group: "Live", step: 0.05 },
  live_stake_frac: { label: "Live size = wallet ×", help: "0.05 = each live order is 5% of the live USDC balance (0 = use the paper × setting above).", group: "Live", step: 0.01 },
  live_loss_limit_usd: { label: "Live loss limit ($, 0 = off)", help: "Settled losses plus $ in open windows; no new live order once it would pass this.", group: "Live", step: 5 },
  live_daily_loss_usd: { label: "Live daily loss limit ($, 0 = off)", help: "The same, counting only windows started today (UTC); resets at 00:00 UTC.", group: "Live", step: 5 },
  live_max_order_usd: { label: "Max $ per live order", group: "Live", step: 5 },
};
const GROUPS = ["Live", "Signal", "Exit", "Quoting", "Risk", "Simulation"] as const;

export function BotSettings({ bot, onClose }: { bot: any; onClose: () => void }) {
  const [cfg, setCfg] = useState<Record<string, any>>({ ...bot.config });
  const groups = GROUPS.filter((g) => Object.keys(bot.config).some((k) => (META[k]?.group || "Simulation") === g));
  const [tab, setTab] = useState<(typeof GROUPS)[number]>(groups[0] || "Quoting");
  const keys = Object.keys(cfg).filter((k) => (META[k]?.group || "Simulation") === tab);
  async function save() {
    const changes: Record<string, any> = {};
    for (const [k, v] of Object.entries(cfg)) {
      if (JSON.stringify(v) !== JSON.stringify(bot.config[k])) changes[k] = v;
    }
    try {
      await api.put(`/api/bots/${bot.id}/config`, changes);
      toast("Settings saved" + (bot.running ? " and applied to the running bot" : ""), "ok");
      onClose();
    } catch (e: any) { toast(e.message, "err"); }
  }
  return (
    <Modal title={`${bot.name} · settings`} sub="Applied to the running bot immediately and saved for next time." onClose={onClose} wide
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save}>Save</button></>}>
      <Tabs value={tab} options={groups} onChange={setTab} />
      <div className="grid2" style={{ gap: 14 }}>
        {keys.map((k) => {
          const v = cfg[k], m = META[k] || { label: k, group: "Simulation" };
          if (typeof v === "boolean") {
            return <div key={k} className="row" style={{ gridColumn: "1 / -1" }}><Switch on={v} onChange={(x) => setCfg({ ...cfg, [k]: x })} /><div><div>{m.label}</div>{m.help && <div className="muted small">{m.help}</div>}</div></div>;
          }
          if (m.options) {
            return <Field key={k} label={m.label} hint={m.help}>
              <select className="input" value={v} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })}>
                {m.options.map(([val, text]) => <option key={val} value={val}>{text}</option>)}
              </select>
            </Field>;
          }
          if (Array.isArray(v)) {
            return <Field key={k} label={m.label} hint={m.help}>
              <input className="input" defaultValue={v.join(", ")} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value.split(/[,;\s]+/).filter(Boolean).map((x) => (isNaN(Number(x)) ? x : Number(x))) })} />
            </Field>;
          }
          if (typeof v === "string" && isNaN(Number(v))) {
            return <Field key={k} label={m.label} hint={m.help}>
              <input className="input mono" value={v} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value.trim() })} />
            </Field>;
          }
          return <Field key={k} label={m.label} hint={m.help}>
            <input className="input" type="number" step={m.step ?? "any"} value={v} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value === "" ? "" : Number(e.target.value) })} />
          </Field>;
        })}
      </div>
    </Modal>
  );
}

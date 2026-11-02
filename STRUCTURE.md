# STRUCTURE.md — project map

Read this instead of crawling the repo. It says where everything lives, how the pieces talk, and where a
change belongs. **Keep it current:** any change that adds/moves a module, table, route, page or data path
updates this file in the same change.

Last full revision: 2026-10-21 (v2 restructure: Streamlit → FastAPI + React, files → SQLite).

---

## 1. What the app is

A local app (one Windows PC) for Polymarket's crypto **Up or Down** short-window markets
(BTC 5m/15m, also ETH/SOL/XRP for trader analysis). Four jobs:

| Area | What it does | Code |
|---|---|---|
| **Trade** | Manual live trading with the wallet in `.env.live`, lowest possible latency | `backend/trading/`, `frontend/src/pages/trade/` |
| **Bots** | Many bots, each its own process + paper account; all actions stored | `backend/bots/`, `frontend/src/pages/bots/` |
| **Traders** | Track other wallets: download history, analyse the strategy, watch live | `backend/traders/`, `frontend/src/pages/traders/` |
| **Storage** | SQLite for everything we own; files only for bulky derived data | `backend/db.py`, `data/` |

Market data itself (books, ticks) is **not** archived here — a separate PC records it.

## 2. Run / build

| Task | Command (from project root) |
|---|---|
| Start everything | `run_app.bat` (creates `.venv`, installs, builds UI if missing, opens http://127.0.0.1:8000) |
| Start server only | `.venv\Scripts\python -m backend` (`--no-browser` to skip opening a tab) |
| Rebuild UI | `cd frontend && ..\.tools\node\npm.cmd run build` (≈1 s) |
| UI rebuild on save | `cd frontend && ..\.tools\node\npm.cmd run watch`, then refresh the browser |
| Type-check UI | `cd frontend && ..\.tools\node\npx.cmd tsc --noEmit` |
| CLI | `.venv\Scripts\python -m backend.cli -h` (traders, add, update, analyze, live, export, bots, bot start/stop/archive/unarchive) |
| One bot by hand | `.venv\Scripts\python -m backend.bots.runner <bot_id>` |
| Record market data | `.venv\Scripts\python -m backend.recorder` (keep running; replays need it) |
| Replay bots offline | `.venv\Scripts\python -m backend.bots.replay --hours 6 --bot crouch-v2 --variant crouch_v2:maker_min_edge=0.01` |

Node is portable in `.tools/node/` (not on PATH, not system-installed).

**Windows Application Control (Smart App Control) is ON on this PC.** It blocks unsigned native binaries
(`.node`, some `.dll`/`.pyd`). Consequences already handled — keep them in mind when adding dependencies:
* the UI is bundled with **esbuild-wasm** (`frontend/build.mjs`), not Vite/rolldown/native esbuild;
* `pyarrow.dataset` is blocked → never use `filters=`/datasets; use `util.read_parquet_where()` (row-group
  min/max pruning) or plain `pd.read_parquet`;
* signed wheels work (pyarrow core, coincurve, orjson, uvicorn's httptools). Test-import any new native dep.

## 3. Directory tree

```
backend/                     Python package (server, engines, analysis). Entry: python -m backend
  __main__.py                starts uvicorn on HOST:PORT (.env, default 127.0.0.1:8000) + opens browser
  config.py                  ALL paths, endpoints, env loading. Never hard-code a path/URL elsewhere
  db.py                      SQLite schema + helpers q()/one()/x()/many()/dumps()/loads()
  util.py                    atomic writes, read_parquet_where, NTP offset, pid/process helpers, pct
  jobs.py                    JobManager: trader download/analysis in a CHILD PROCESS (keeps GIL free)
  recorder.py                market-data recorder for bot research (python -m backend.recorder): raw CLOB books of
                             every BTC 5m/15m window, Coinbase, Kraken, Binance spot + futures, Chainlink, RTDS BTC
                             Up/Down activity -> data/recordings/
  cli.py                     command line
  polymarket/markets.py      ASSETS, window math (window_bounds/window_slug/slug_start), Gamma lookups
                             (fetch_market, fetch_winner), historical slug/title parsing (market_map)
  polymarket/profile.py      a wallet's public profile (Profile page): Gamma public-profile + Data API v2 user-stats,
                             user-pnl, leaderboard (timePeriod=all), positions, activity (cursor paging); cached a few s.
                             user-pnl-api / lb-api hosts are retired (410)
  traders/
    store.py                 Trader dataclass (DB row) + per-trader files + shared caches (resolutions, klines)
    sources.py               remote fetches: data-api activity paging, handle lookup, CLOB resolutions, Binance 1s
    pipeline.py              run_update(): activity → resolutions → klines → analysis (job-aware, cancellable)
    analysis.py              strategy analysis → summary.json, report.md, tables, windows.csv, buys.parquet
    export.py                zip export of one/many traders
    live.py                  LiveHub: RTDS trade stream filtered by wallet, Chainlink, CLOB books, polling;
                             events of watched traders → DB trader_events. Always started with the server: its
                             Chainlink stream supplies exact window open/close prices (exact_price)
  bots/
    registry.py              STRATEGIES dict + bot CRUD (DB table bots)
    control.py               start/stop/config/reset a bot process; equity; latency report
    runner.py                process entry: python -m backend.bots.runner <bot_id> (SIGTERM = clean stop)
    replay.py                offline replay of data/recordings through any bot configs (same engine code, virtual
                             clock, scored on official winners): python -m backend.bots.replay --hours 6 --bot <id>
                             (--dir <folder>: another recordings folder, e.g. data/import/btc5m/recordings_dublin)
    latency.py               measured delays for every simulation: Latency(site).draw/pick(key, best|typical|worst),
                             fak_match_ms (taker delay model), maker_times. Data: latency_profile.json (config
                             LATENCY_PROFILE, built by scripts/latency_build.py). Keys + use: docs/latency/README.md
    strategies/market_maker.py   PaperBot engine + rebate market maker (CrouchPredictions reconstruction).
                             Subclass hooks: CONFIG, STRATEGY, _side_quotes(), _blocked(), _maybe_take()
    strategies/ml_maker.py   MLBot: machinelearned reconstruction (calibration notes in its docstring)
    strategies/join_copy.py  JoinCopyBot: copies a wallet's fills live (RTDS activity stream)
    strategies/crouch_v2.py  CrouchBot: CrouchPredictions followed from its live fills (one level, sized by price,
                             momentum takes); calibration in its docstring
    strategies/fast_player.py  FastBot: latency taker (docs/bots/fast_player/FAST_PLAYER_SPEC.md rebuilt): Coinbase/Kraken/Binance spike ->
                             FAK buy before the book reprices; hold or scalp exit; FAK fills on the book at match time
                             (Polymarket clock), own Kraken + Binance futures feeds, sells, signal log (`signals`).
                             `signal_feeds` preset (FEED_PRESETS): binance_fut | classic (cb + kr + bn mean) | custom
                             (own sources/combine); a settings change starts any feed it needs on the running bot.
                             Live mode (bots.mode='live', read at start): own Gateway, real FAK buy of paper $ x
                             live_scale (or live_stake_frac x live USDC) next to each paper entry, live ledger (state `live`; rebuilt from
                             live_orders by origin, incl. the order log with sign/post ms), live_loss_limit_usd (0 = off) and
                             live_daily_loss_usd (windows started today, UTC) count open $ as lost,
                             live_paused = kill switch. State `live` also has fill counts and `pnl_series` (cumulative settled PnL)
    strategies/late_fav.py   LateFavBot: resting 97-99c bids on the near-certain side in a window's last 2 min, held
                             to settlement, pulled on fast BTC moves (from the old PC's study, reports/). v2 (gate
                             "lock"): bids only once the final TWAP is locked (z + $ margin); queue_cancel_credit off =
                             pessimistic queue. Live (mode live): the paper quotes mirrored as real post-only
                             bids via the Gateway (live_size, live_budget_usd, live_loss_limit_usd, live_paused)
    strategies/guard_maker.py  GuardMakerBot: two-sided post-only bids around fair (PaperBot quoting) + the fast
                             player's spike signal (subclass of FastBot): a spike cancels the threatened side at once
                             and keeps it out for pull_ms. Own cancel latency; prints judged at their match time
    strategies/stale_taker.py  StaleTakerBot (subclass of FastBot), paper only: Binance futures 1 s move >= spike_bps
                             -> FAK buy of the favoured token, 10 shares at ask + 2 ticks, max 10 per window, held to
                             settlement; each order's latency drawn from fast-value-live's real FAK latencies
                             (LATENCY_Q, p50 ~0.7 s). Mode live ignored. docs/bots/stale_taker/README.md
    strategies/imb_maker.py  ImbMakerBot (subclass of GuardMakerBot), paper only: docs/bots/bot_sim/BOT_SPEC.md ported
                             from its strategy.py (rule_size, BinanceGuard, top_depths, ladders): 13-level ladders on
                             both tokens (levels below min_price end a ladder, above max_price are skipped), top joins the best bid at queue share >= 0.4 else 2 ticks back, Binance
                             futures guard on every trade (no quotes while the guard feed is stale), net cap 300. Own
                             _requote (no price-model gate). Per-window logs -> data/bots/<id>/windows/ (see §5).
                             value_guard (2026-11-02): a side quotes only while BidValueModel (config.BID_VALUE_MODEL,
                             JSON trees, plain Python) predicts its best bid's 10 s value >= value_cut (cents);
                             value_model picks another JSON next to it. sticky_ticks (paper / replay only, n_levels 1):
                             a resting bid up to N ticks below the wanted price keeps its queue place.
                             Live (mode 'live', read at start; spec phase 2): the same decisions with the live
                             window's own inventory drive real post-only orders via its own Gateway (batch post /
                             cancel, fills from user-channel maker_orders by order id, ledger rebuilt from
                             live_orders), kill switch live_paused, live_loss_limit_usd (worst case of open windows),
                             spend_cap_usd per window, merge of pairs after each window, cancel-all on stop and of
                             leftovers at start. live_dry_run (paper mode): the whole live flow, nothing sent.
                             live_requote_wait_ack: a re-priced order is cancelled once its post is answered
                             (Polymarket holds a post's answer 0.6-9 s when its cancel arrives first; urgent cancels
                             still go in flight); live_max_inflight caps posts awaiting an answer; a post lost in
                             transit stays tracked ('unconfirmed'), a reconcile re-adopts 'rejected' orders still open;
                             live_stall_ms: no new posts while the market's feed is silent / a post waits that long
                             (Polymarket's exchange-wide pauses); spend_cap_hard: past the cap only until flat once
  trading/gateway.py         Gateway singleton on the official SDK `polymarket-client` (SecureClient, CLOB V2 orders,
                             auto wallet type), arm/limits, place/cancel, approvals, user-channel fills,
                             balances/positions, market pre-warming. Maker path (no refreshes per order):
                             post_limit_batch (<= 15 post-only BUYs), cancel_batch, merge_max; on_event hook.
                             Pools: pool (orders), cpool (bot cancels, never behind posts), bg (refreshes, merges)
  trading/fastorders.py      FastOrders on a Gateway's client: order id = EIP-712 hash computed at signing (checked
                             against the exchange's), one order per request on the warm HTTP/2 connection (48
                             threads), cancels by id at once on their own 32 threads, also while the post is in
                             flight (the exchange then refuses the late post: "Already canceled"). Used by imb_maker
                             live_fast. Probe 2026-10-26: order / cancel on the book ~15-20 ms after sending.
                             cancel_gw: cancels on a second Gateway's client (own connection), resent once when the
                             connection drops; post errors classified (code transport = sent, may rest; rate_limited);
                             `rl` = Poly-RateLimit-* headers seen (tier, min remaining, warnings, 429s)
  api/
    app.py                   FastAPI app, lifespan (autostart watches, gateway), static SPA serving
    state.py                 server singletons: jobs (JobManager), hub (LiveHub), records() helper
    routes/system.py         /api/status, /api/jobs*
    routes/traders.py        /api/traders*, /api/export
    routes/bots.py           /api/strategies, /api/bots*
    routes/trading.py        /api/trade/*, websocket /ws/trade
    routes/profile.py        /api/profile* (our trading wallet = gateway.wallet; never reads .env.live)
frontend/                    React 19 + TypeScript, bundled by esbuild-wasm into frontend/dist (served by FastAPI)
  build.mjs                  build / --watch (copies public/ into dist/, adds ?v=<build> to the css/js links)
  index.html                 shell (build.mjs rewrites the script tag into dist/index.html); PWA meta (apple-* tags, manifest)
  public/                    served at the site root: manifest.json (standalone home-screen app) + PNG app icons
  src/main.tsx, App.tsx      router + top bar (gem brand, pill nav, jobs popover, trading status chip) + bottom tab bar on phones (<=760px: Trade, Bots, Traders, Profile; the Jobs button shows in the top bar only while a job runs)
  src/styles.css             THE design system (tokens + every component class). Opal-style dark look: black + iridescent aura, pill controls, white primary buttons, modals = bottom sheets on phones
  src/lib/api.ts             fetch JSON helpers (api.get/post/put/patch/del)
  src/lib/format.ts          money/pct/num/cents/clock/ago/... formatting
  src/lib/hooks.ts           usePoll(url, ms), useNow(ms), usePref(key) (localStorage UI prefs only)
  src/lib/feeds.ts           browser-direct market data: Chainlink/Coinbase/Kraken/Binance + CLOB books
  src/lib/tradeSocket.ts     the /ws/trade client (orders out, account events in, RTT)
  src/components/ui.tsx      Card, Kpis, Strip, Tabs, Seg, Modal, toast, Table, Pill, Chip, Switch, Ring (Opal progress ring: trade countdown, bots win rate), ...
  src/components/charts.tsx  LineChart (uPlot; `smooth` overlays movingAvg() of each series: bot + trader P/L charts),
                             Bars (SVG), histogram(), palette C
  src/components/Sparkline.tsx  small SVG value line + fading area for cards (bot cards: portfolio over 7 days)
  src/pages/trade/           TradePage, PriceChart, OrderBook, OrderTicket, AccountPanel, WindowPositions (card under the chart), useTrade, positions (live P/L)
  src/pages/bots/            BotsPage (cards), BotPage (dashboard), BotSettings (config form)
  src/pages/traders/         TradersPage (compare), TraderPage (tabs), sections.tsx, LiveSection, dialogs
  src/pages/profile/         ProfilePage: our Polymarket profile (pfp, bio, stats, rank, P/L chart + breakdown, positions,
                             closed positions, activity with Load more)
data/                        runtime data (gitignored) — see §5
scripts/migrate_v1.py        one-time v1→v2 migration (already run 2026-10-21; kept for reference)
scripts/import_btc5m.py      old logger PC's SQLite (data/import/btc5m/raw_db) -> recorder format for replay
                             (--timing home|dublin: as received at home, or re-timed to Dublin's feed latencies)
scripts/geotest_order.py     one post-only $0.05 order + cancel: checks a server location may trade (run by the user)
scripts/maker_latency_test.py  N post-only $0.05 orders + cancels: round trips and when each shows on / leaves the
                             market feed (= takes effect at the engine). Run on the server
scripts/fast_value_live.py   server: create bot fast-value-live (fast-value's settings) / arm / disarm (mode live|paper)
scripts/fast_london_live.py  server: bot fast-london: create / arm / disarm / signal binance_fut|classic (live switch)
scripts/fast_source_replay.py  fast player replay per BTC feed (each alone / combined, queue + 50 ms models; --retime)
scripts/source_leadlag.py    which BTC feed moves first as received (cross-correlation, spike events, Polymarket mid reaction)
scripts/order_speed_probe.py server, REAL orders that cannot fill (5.37 sh at 0.02 on the next window, cancelled at
                             once): post / cancel / in-flight-cancel timings on our clock and the book's -> data/probe/;
                             --site <s> also writes data/latency/orders-<s>-*.json for latency_build; --summarize
                             <file> re-reads a saved run (no orders)
scripts/imb_london_replay.py  Phase 3 of LONDON_PLAN: imb_maker configs x fill models (queue / pq pessimistic queue / tt
                             trade-through) x latency on recordings re-timed to London; 2 h slices in parallel, mk1/mk10
                             markouts; -spikeN / -emp cancel latency (fixed during guard spikes / drawn per cancel from the stress probe); FairGuard
                             variants; --set ladder (13-level / Crouch-size ladders), --native (London recordings, no
                             re-timing), --set sweep (-swNN / -vNN / -hi / -th / -td quote guards: imb_maker's value guard;
                             -stN sticky quotes);
                             per variant also pair% / pair cost / paired vs unpaired PnL
                             -> data/replays/imb_london-*.json (places no orders)
scripts/fair_calib.py        extract (per 5m window, samples at fixed seconds: strike = our Chainlink TWAP, futures +
                             basis, TWAP seen so far, realized vol, Up mid, winner) / fit (TWAP random-walk P(Up) vs
                             the market mid: log-loss, blend, value of disagreements) -> data/forensics/calib-<day>
scripts/follow_scan.py       would following a studied trader pay: their 5m BUYs re-bought D = 1 / 5 / 10 s later at the
                             mid + half a tick + taker fee, to settlement (on trader_forensics' fills files; read-only)
scripts/endgame_scan.py      every 0.5 s of the last 40 s of each 5m window: causal Chainlink TWAP projection, z vs the
                             strike, ask / depth of the projected winner -> data/forensics/endgame-<day>; report
scripts/sweep_predict.py     extract (every 200 ms per token: best-bid level features + "level gone within 50..1000 ms"
                             label + mid 1 s / 10 s later) -> data/forensics/sweep-<day>[-hh-hh].npz / fit (gradient
                             boosting, AUC, cancel thresholds) -> data/forensics/sweep_model.pkl (needs sklearn) /
                             export (a pickled regressor -> backend/bots/<name>.json); extract also labels `hit` (shares
                             printed into the level) and reads tokens from the recording's info lines
scripts/resting_probe.py     server, 3 REAL post-only bids that cannot fill (5 sh at 0.01-0.03, next window), status
                             polled every 1 s, cancelled after 60 s: do resting orders survive (the 10-27 ~16 s
                             account-wide cancels)? -> data/probe/resting-*.json
scripts/delay_probe.py       server, REAL orders that cannot fill (0.02, active + next window): per request httpx
                             send / answer times, Poly-RateLimit headers, book appearance, and (--pcap, tcpdump on the
                             box) the answer's first packet on the wire; tests seq / burst / bot-like load / cancel in
                             flight on a second connection -> data/probe/delay-*.json. Proved the held answers (10-27)
scripts/check_imb_maker.py   offline check of imb_maker against docs/bots/bot_sim/strategy.py + the spec's cases,
                             and of its live layer against a fake gateway (python -m scripts.check_imb_maker)
scripts/check_stale_taker.py offline check of stale_taker's entry rules, latency draw and paper fill
scripts/imb_maker_live.py    server: create bot imb-maker-live (dry run) / dry / arm / disarm
scripts/imb_london_live.py   server: create bot imb-london [base|guard03|fair1|fair05|value0] (dry run, $10 bankroll, $8 live
                             loss limit) / set <variant> / dry / arm / disarm; --bot <id> for another bot (LONDON_PLAN Phase 4)
scripts/imb_london_status.py server: one status line of a live imb_maker bot + its twin from state.json (exit 3 = attention)
scripts/trader_forensics.py  fetch (data-api: every BTC Up/Down fill of the 4 studied traders + ours, maker/taker) /
                             extract (recordings: match time via the last_trade_price tx hash, book before/after, mids
                             +1/5/10/30 s, Binance futures path -5..+2 s) / report / deep (taker reaction lag, maker
                             fills by BTC move and by queue outcome) -> data/forensics/{api,fills}-<day>.jsonl.gz
scripts/cancel_stress_probe.py server, REAL unfillable bids (0.01, active window): cancel latency calm vs at Binance
                             spikes, off the book on the exchange clock -> data/probe/stress-*.json (used by the replay's
                             -emp variants)
scripts/late_fav_v2.py       server: create the late_fav lock-gate paper bots (v2, v2-ladder, v3, v3-safe, v3-big)
scripts/late_fav_live.py     server: bot late-fav-v3-live (live_size 25, $50 budget, $20 loss limit): create / dry / arm / disarm
scripts/latency_probe.py     live, no orders: network (DNS/TCP/TLS/keep-alive HTTP per venue) + feed ages/gaps/pings,
                             one lean process per feed -> data/latency/probe-<site>-*.json (--site dublin|home|london)
scripts/latency_history.py   server: recordings + live_orders + imb window logs -> feed ages as the bots got them, maker
                             reprice after Binance moves, FAK/post-only/cancel/fill-notice timings -> data/latency/history-*.json
scripts/latency_build.py     merges the newest probe/history files -> backend/bots/latency_profile.json + FAK model check
scripts/feed_latency_test.py  standalone feed race (Binance/Coinbase/Chainlink: spike race, cross-correlation, TCP
                             connect, SNTP); places no orders. Output -> reports/latency_result.txt (Dublin, 10-23)
deploy/install.sh            Linux server: install/update systemd services (server, recorder, one unit per bot)
deploy/az_probe.sh           fresh EC2 box: venv + scripts/latency_probe.py labelled by AWS zone id (choosing an AZ)
deploy/systemd/              pmtool-server, pmtool-recorder, pmtool-bot@<id> unit files
docs/latency/README.md       the measured delays: method, every key, numbers, how a sim must use them
docs/polymarket/             Polymarket rules/mechanics research (2026-10-27): README.md (index + ranked findings + our
                             beliefs checked), one file per topic (trading rules, fees/rebates, Up/Down markets, latency,
                             account/KYC, API limits/WS, settlement, programs), changelog_timeline, opportunities,
                             open_questions (support messages), sources. Read before changing order, feed or fee logic
docs/bots/                   the user's write-ups of bot versions: README.md (index + version template), one folder
                             per strategy (<strategy>/README.md = versions table + notes, specs, results/ for small
                             exported results). Add a row there when creating a bot. Runtime data stays in data/ + DB
docs/bots/fast_player/FAST_PLAYER_SPEC.md  spec of the user's earlier btc5m fast bots (fast_player.py rebuilt from it)
docs/bots/bot_sim/           imb_maker's folder: BOT_SPEC.md (the study's build description, phases 1-2) + strategy.py
                             (its reference decision code; imb_maker.py is the port)
docs/bots/imb_maker/         imb_maker live post-mortems (LIVE_POSTMORTEM_2026-10-27.md: imb-maker-10 vs its twin / replay)
docs/TRADER_RE_PLAN.md       reference (suggestions only) for re-doing the CrouchPredictions / machinelearned study:
                             how to collect each data source, analysis options, bot-building options
reports/                     study outputs: ULTIMATE_REPORT.md + ult_*.txt (old logger PC's 64 h study),
                             latency_result.txt (feed race from Dublin)
archive/v1-2026-10-21/       old Streamlit code, raw v1 files (profiles/, bot_data/, legacy data/) — read-only
.tools/node/                 portable Node.js 24 (UI build only)
run_app.bat                  launcher
.env / .env.live             settings + secrets (gitignored; see .env.example / .env.live.example)
spikebot/                    Standalone spike-taker bot (not part of the app; README "Spike-taker paper bot"): Binance spot
                             1.5 bps / 1 s -> buy the favoured BTC 5m token's stale ask 0.15-0.60, hold to settlement.
                             Paper by default (self-contained: websockets + requests, logs/ + logs/spikebot.db); variant
                             `live` sends real FAK buys through backend/trading/gateway.py (runs in ~/app on the server)
research/                    (git-ignored, deletable) the 2026-10-27 @hcrystallash / @wzbbb study behind spikebot
logs/                        (git-ignored) spikebot output, one folder per non-main variant

```

## 4. Processes and data flow

```
                 ┌───────────────────────── browser (React) ─────────────────────────┐
                 │ Trade page: WebSockets DIRECT to Coinbase, Kraken, Binance,       │
                 │   Polymarket RTDS (Chainlink) and CLOB market books               │
                 │ /ws/trade  (orders, cancels, account events)  /api/* (REST, poll) │
                 └──────────────┬───────────────────────────────┬────────────────────┘
                                │                               │
     ┌──────────────────────────▼───── server: python -m backend ▼─────────────────────┐
     │ FastAPI/uvicorn (one event loop)                                                 │
     │  • trading.gateway  → ClobClient (warm HTTP/2) → clob.polymarket.com             │
     │                      ← CLOB user channel ws (our fills/orders)                   │
     │  • traders.live.LiveHub (threads) ← RTDS trades/Chainlink, CLOB books, data-api  │
     │  • jobs.JobManager → spawns child process per download/analysis                 │
     │  • bots.control    → spawns/stops bot processes, reads their state.json          │
     └───────┬───────────────────────────────┬─────────────────────────────────────────┘
             │ SQLite (WAL) data/btc5m.db    │ files data/
     ┌───────▼───────────────┐      ┌────────▼─────────────────────────────────────┐
     │ bot process per bot    │      │ job child process (pandas-heavy analysis)    │
     │ backend.bots.runner    │      │ backend.jobs._child → traders.pipeline       │
     │ own feeds + asyncio    │      └──────────────────────────────────────────────┘
     │ writes DB + state.json │
     └────────────────────────┘
```

Why separate processes: Python's GIL. Order sending, bot feed handlers and pandas analysis must never
wait on each other.

## 5. Storage

### SQLite — `data/btc5m.db` (path = `DB_PATH` in `.env`, relative to project root)
WAL mode; every process/thread opens its own connection via `db.conn()`. Schema lives in `db.SCHEMA`
(add tables there; `CREATE TABLE IF NOT EXISTS` runs on first connect).

| Table | Key | Written by | Contents |
|---|---|---|---|
| `traders` | id | traders/store.py | tracked wallets: name, wallet, asset, window, coverage (JSON), notes, flags |
| `trader_events` | id | traders/live.py | live-captured trades/redeems of watched traders (`data` = full event JSON) |
| `bots` | id | bots/registry.py, control.py | strategy, mode (paper/live), config JSON, current `account` number, `archived` (time, else NULL: hidden from the Bots page and its totals, start refused, runner exits 0; column added to old DBs by `db.ADDED_COLUMNS`). Bots: `crouch-mm` (market_maker), `ml-copy` (ml_maker; `ml-join` (join_copy) deleted 2026-10-23), `crouch-v2` (crouch_v2), `fast-hold` / `fast-scalp` / `fast-value` and `fast-hold-v2` / `fast-scalp-v2` / `fast-value-v2` (fast_player), `late-fav` (late_fav), `late-fav-v2` / `late-fav-v2-ladder` / `late-fav-v3` / `late-fav-v3-safe` / `late-fav-v3-big` (late_fav gate "lock", paper, scripts/late_fav_v2.py), `imb-maker` / `imb-maker-300` (imb_maker; $5k B 60 / $300 B 5 cap 25; Dublin since 2026-10-25), `imb-maker-live` (imb_maker live, scripts/imb_maker_live.py; archived), `imb-maker-10` (imb_maker live, $10 measurement run, 2026-10-27), `imb-london` (imb_maker, guard_mode fair, scripts/imb_london_live.py; live 2026-11-01 12:24-12:53Z, paper since), `imb-london-live` (imb_maker LIVE on London 2026-11-01 18:37Z, stopped; since 2026-11-02 07:29Z variant value0 = value guard 0 c, always join, $7.10 lifetime live loss limit), `stale-taker-1bps` / `stale-taker-2bps` (stale_taker, $1k paper, 10 sh; Dublin since 2026-10-26) (guard_maker bots `guard-mm-5m` / `guard-mm-15m` deleted from the server 2026-10-23) |
| `bot_fills` | id; (bot_id, account, ts) | bot process | every (paper) fill incl. maker/taker, rebate, markout. Sells (fast_player scalp) have negative `shares` and `usd` (= net cash received) |
| `bot_windows` | (bot_id, account, slug) | bot process | per-window settlement record (`data` JSON), upserted until official |
| `bot_equity` | (bot_id, account, ts) | bot process | portfolio value every 30 s (PnL chart) |
| `bot_changes` | id; (bot_id, ts) | registry.add_change (create, control.save_config, control.reset, control.reset_day, notes) | per-bot history: created / rebuilt / audit / simulator / settings / account / daily_reset / note; shown in "About this bot" at the bottom of the bot page. The engine restores today's latest `daily_reset` on restart (daily loss counts from the equity at that moment) |
| `live_orders` | order_id | trading/gateway.py | every order sent from this app (origin=manual or bot id), latency, errors |
| `live_trades` | id | trading/gateway.py | our fills from the CLOB user channel |

"New account" on a bot = `bots.account += 1`; old rows stay under the old number.

### Files — `data/`
| Path | What | Notes |
|---|---|---|
| `data/traders/<id>/activity.parquet` | raw activity of the wallet (all markets) for covered ranges | rewritten per download |
| `data/traders/<id>/analysis/` | `summary.json`, `report.md`, `windows.csv`, `timing/price/hourly/daily/edge_deciles.csv`, `buys.parquet` | regenerated each analysis; `buys.parquet` sorted by slug, 20k-row groups |
| `data/traders/<id>/last_job.log` | last job log | |
| `data/bots/<id>/state.json` | live snapshot, 2×/s | read by API `/api/bots/<id>/state` |
| `data/bots/<id>/control.json` | commands to the bot | `{"id", "action": stop/reset/reset_day/config}` |
| `data/bots/<id>/bot.pid`, `bot.log` | process id / output | errors are written here (one line per kind per 30 s) |
| `data/bots/<id>/windows/<UTC day>/<slug>.json.gz` | imb_maker: one JSON per window (orders, fills, guard triggers, decisions as `{cols, rows}`, + cfg, winner, shares) | written at settlement, ~80-150 KB each; days older than 30 pruned |
| `data/cache/resolutions.json` | market slug → winner | shared by traders |
| `data/cache/klines/<SYMBOL>/<day>.csv` | Binance 1 s closes per UTC day | shared by traders |
| `data/forensics/api-<day>.jsonl.gz`, `fills-<day>.jsonl.gz` | studied traders' + our BTC fills (data-api) and their per-fill markouts from the recordings | scripts/trader_forensics.py |
| `data/forensics/calib-<day>*`, `endgame-<day>*`, `sweep-<day>*.npz` | fair-value, endgame and sweep-predictor samples | scripts/fair_calib.py, endgame_scan.py, sweep_predict.py |
| `backend/bots/bid_value_model.json` | the 10 s bid-value regressor as JSON trees (committed) | scripts/sweep_predict export / read by imb_maker's value guard |
| `backend/bots/bid_value_hit.json`, `bid_value_thru.json`, `bid_value_thru_d4.json` | the same regressor trained on hit samples / on samples whose prints got through the queue ahead (Dublin + 11-01 / Dublin only); picked with imb_maker's `value_model` | scripts/sweep_predict export <pkl> <name> |
| `data/forensics/sweep_model.pkl`, `hit_model.pkl`, `thru_model.pkl`, `thru_d4_model.pkl` | {"model": sweep classifier, "reg": 10 s bid-value regressor, "train": days}; the given-hit / through-queue regressors | sweep_predict fit / read by imb_london_replay -sw / -v variants |
| `data/recordings/<YYYY-MM-DD>/<HH>.tsv.gz` | raw feeds, one line `recv_ms<TAB>source<TAB>message` (source cb = Coinbase ticker, or `matches` messages when Coinbase refused the ticker, bn, bf = Binance futures aggTrade (bookTicker 14:32-14:40Z on 10-22), kr, cl, act, info, bk:<slug>) | written by backend/recorder.py (~4 GB/day, 7 days kept; one gzip member per recorder start, closed on SIGTERM); read by bots/replay.py (`_gz_lines` also recovers hours where a killed recorder left a member unterminated) |
| `data/recordings/recorder.log` | recorder output | |
| `data/latency/probe-<site>-<utc>.json`, `history-<utc>.json`, `orders-<site>-<utc>.json` | raw latency measurements (summaries + 101 quantiles); `aztest/` = the 11-01 London AZ comparison (not merged) | scripts/latency_probe.py / latency_history.py / order_speed_probe.py --site; merged by latency_build.py into backend/bots/latency_profile.json (committed) |
| `data/import/btc5m/` | the old logger PC's export (2026-10-19..22, 64 h): `INVENTORY.md` (read first), `raw_db/btc5m.db` (all feeds, its paper bots, windows), `bot_dbs/` (its live bots), `code/` (old project + analysis, `analysis/data/*.pkl` need pandas >= 3), `extracts/` | read-only input |
| `data/import/btc5m/recordings_{home,dublin}/` | that data converted to the recorder format by scripts/import_btc5m.py | replay with `--dir` |

## 5b. Production server (Linux, AWS London)
Since 2026-11-01 the app runs on AWS EC2 in London: `pmtool-london` (i-02cd06aeb8754c488, eu-west-2c = zone id
euw2-az1, c7i.xlarge, Ubuntu 24.04, 100 GB gp3 that survives termination, 4 GB swap), public IP 18.169.243.198
(changes on stop/start; no Elastic IP), SSH `ssh -i dublin-key-aws.pem ubuntu@<ip>` (EC2 key pair `pmtool-key` =
the same key), security group `pmtool-ssh` (port 22 from the user's home IP only). Polymarket whitelisted the wallet
for eu-west-2 (co-location); `polymarket.com/api/geoblock` still says blocked (GB) for the IP. CLOB kept-alive HTTPS
RTT ~14-16 ms (Dublin ~22), book feed age p50 ~5 ms (Dublin ~9); AZ test: `deploy/az_probe.sh`, results in
`data/latency/probe-london-euw2-*`. The old Dublin server (eu-west-1, i-094f7c59e17b8b2c6, 2026-10-22..11-01) is
being retired (no live orders from Dublin; stop it once London is verified). Code lives in `~/app` (same layout),
deployed by copying the project without `.env*`, `*.pem`, `data/`, `archive/`. The recorder keeps 7 days and
deletes older days sooner while the disk has < 10 GB free (`recorder.MIN_FREE_GB`). systemd runs
`pmtool-server` (UI on 127.0.0.1:8000 only; open it through an SSH tunnel), `pmtool-recorder` and
`pmtool-bot@<id>` (Nice −5, restarted on crash, a UI Stop stays stopped: the bot exits 0 within ~2 s because feed
sockets close with `market_maker.WS_CLOSE_S`; if it hangs, `control.stop` sends SIGTERM, which systemd's
`Restart=on-failure` treats as clean, and only then SIGKILL, which it treats as a crash). Install/update:
`bash deploy/install.sh <bot ids>`. Logs: `journalctl -u pmtool-server`, `data/bots/<id>/bot.log`,
`data/recordings/recorder.log`. On Linux, uvloop is used and bots started from the UI detach from the server.
A bot refuses to start twice (runner checks bot.pid). The server's DB is its own copy: results there and on
the home PC are separate.

## 6. Secrets and settings
* `.env` — `POLY_API_KEY/SECRET/PASSPHRASE` (CLOB L2 creds of the trading wallet; used by the gateway),
  `DB_PATH`, optional `HOST`, `PORT`.
* `.env.live` — `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_WALLET_ADDRESS` (this account: a DEPOSIT_WALLET),
  `POLYMARKET_BUILDER_API_KEY/SECRET/PASSPHRASE` (BuilderApiKey for the SDK), optional
  `LIVE_MAX_ORDER_USD` (default 500). The wallet type is detected by the SDK; no signature-type setting.
* The old `py-clob-client` (v1) is archived by Polymarket and its orders are rejected ("invalid order
  version"). Use only `polymarket-client` (pinned in requirements.txt; it needs websockets < 16).
* Only `config.live_env()` reads `.env.live`, only `trading/gateway.py` calls it. Never log, print, return
  to the UI, or write secrets to disk/DB.

## 7. API (all JSON; NaN → null)
| Route | Purpose |
|---|---|
| `GET /api/status` | jobs, live-hub status, trading chip (top bar polls every 2 s) |
| `GET /api/jobs`, `POST /api/jobs/{id}/cancel`, `POST /api/jobs/clear` | background jobs |
| `GET/POST /api/traders` | list (with metrics) / create (+ starts download job) |
| `GET/PATCH/DELETE /api/traders/{id}` | detail (incl. summary, running job) / edit / delete |
| `GET /api/traders/{id}/download-preview?start&end`, `POST .../update`, `POST .../analyse` | data jobs |
| `GET /api/traders/{id}/tables`, `/windows`, `/pnl`, `/window/{slug}`, `/report`, `/files` | analysis outputs |
| `POST/DELETE /api/traders/{id}/watch`, `GET .../live` | live watching + live snapshot |
| `GET /api/traders/{id}/export`, `/export-size`, `GET /api/export?ids=` | zip exports |
| `GET /api/strategies`, `GET/POST /api/bots`, `GET/PATCH/DELETE /api/bots/{id}` | bots (list includes `spark`: current account's value, last 7 days, ≤168 points; plus bankroll, fills/maker/taker, volume, fees, windows_done/won from state.json, summed by the Bots page totals strip; `live` = the live account summary for live bots, shown on the card instead of the paper twin; their `spark` is the live settled PnL; the totals strip leaves live bots out) |
| `GET /api/bots/{id}/state`, `/equity`, `/history`, `/latency` | bot data |
| `GET/POST /api/bots/{id}/changes` | strategy description + bot history / add a note |
| `POST /api/bots/{id}/start`, `/stop`, `/reset`, `/reset-day`, `/archive`, `/unarchive`, `PUT .../config` | bot control (`reset-day`: clear today's loss for the daily loss limit, fast_player only; `archive`: stop + hide, data kept; start of an archived bot -> 409) |
| `GET /api/trade/status`, `/markets`, `/account`, `/history`, `/price-to-beat/{slug}` | live account |
| `POST /api/trade/arm`, `/order`, `/cancel-all`, `DELETE /api/trade/order/{id}` | trading (REST mirror) |
| `POST /api/trade/setup-approvals` | send missing wallet approvals (on-chain, gasless) — UI button only |
| `GET /api/profile`, `/api/profile/pnl?interval=1d\|1w\|1m\|all`, `/api/profile/positions?status=open\|closed&cursor=`, `/api/profile/activity?cursor=` | our Polymarket profile: identity + all-time stats/rank (+ gateway cash), P/L points + first/last breakdown, paged positions / activity |
| `WS /ws/trade` | orders: `{op:"order"|"cancel"|"cancel_all"|"ping", cid, ...}` → `{type:"ack", cid, ok, result|error}`; pushes `hello, status, orders, order, trade, balances, positions` |

## 8. Live trading — latency design (`backend/trading/gateway.py`, `frontend/src/pages/trade/`)
* Market data never goes through the server: the browser opens the exchange sockets itself (`lib/feeds.ts`).
  Chart redraws on requestAnimationFrame from ring buffers (no React render per tick).
* Click → `/ws/trade` (already open, ~0.3 ms) → thread pool → `create_market_order`/`create_order` with tick
  size, neg-risk and fee rate pre-cached by `_market_scheduler` before the window opens → sign (≈0.65 ms with
  coincurve; first order per token ≈400 ms, hence pre-warming by signing a throwaway order) →
  `post_order` on the kept-alive connection (pinged every 8 s) → ack. One-tap $ buttons in the ticket send
  market buys directly.
* Market orders are FAK with a worst price computed in the browser from its live book + slippage setting.
* Time on the Trade screen (countdown, window switch, "closed") is NTP-corrected (`clock_offset_ms`; this PC
  runs ~1 s slow). Price to beat = Polymarket's eventMetadata.priceToBeat, else the 60 s TWAP at the open from
  the browser's ticks, else from the server (`/api/trade/price-to-beat`). "Current price" = rolling 60 s TWAP;
  in the last minute the projected final TWAP is shown; the chart has a TWAP line. Shares held = max(wallet balance, data-api
  position); balances are re-read when a fill is MINED/CONFIRMED and a few times after each order, on a
  background pool so refreshes never delay orders. P/L = value at the live best bid − size × avg price.
* Safety: `armed` is False at every server start; orders refused until armed in the ticket;
  `LIVE_MAX_ORDER_USD` per order. Fills/order updates arrive on the CLOB user channel and are stored.
* Measured on this PC: exchange→browser Coinbase ~12 ms, Kraken ~60 ms, Binance ~75 ms, Polymarket books
  ~60 ms, Chainlink ~1.4 s (oracle publish lag); server↔CLOB ~150–300 ms (network).

## 9. Domain rules that matter
* Window slug `<asset>-updown-<5|15>m-<start epoch>`; window = [start, start + minutes·60).
* Settlement: Chainlink's 60 s TWAP stream (`btc-usd-twap-60s`) at the window start (price to beat) and end
  (final price); Up wins if final ≥ price to beat. Not on RTDS → computed from the 1 s btc/usd ticks as the
  mean of the ticks observed in [B−62, B−3] (`polymarket/markets.py:twap_secs` / `twap_of`, `lib/feeds.ts:twapAt`;
  fitted 2026-10-27: exact to the cent in ~half of the windows, median 2¢; the outcome is fixed 3 s before the end).
  Polymarket's own values: Gamma **event** `eventMetadata.priceToBeat` / `finalPrice` (`fetch_window_prices`;
  priceToBeat appears only after the previous window resolved). The single boundary tick and
  polymarket.com/api/crypto/crypto-price (= that tick) get ~12% of windows wrong: don't use them.
* Up and Down books mirror (Up bid p ≡ Down ask 1−p).
* Maker rebate estimate = shares × fee_rate × p(1−p) × rebate_share (0.15 calibrated).
* Paper fills (`market_maker.py`): queue ahead shrinks only by prints and pro-rata cancels; a book shrink and its
  print arrive in either order and are matched within `LEVEL_MATCH_MS`; order latency 200 ms (measured round
  trips 150–310 ms). The CLOB feed sends one `last_trade_price` per taker order (total size, one price).
  Engine hooks `_cancel_lat()` (cancel delay, default latency_ms) and `_print_ms()` (when a print is judged,
  default on receipt; guard_maker uses its exchange timestamp).
* Order timing from Dublin (2026-10-23, scripts/maker_latency_test.py): post-only orders live on the book ~26 ms
  after sending (p90 ~35), cancels take effect ~26 ms (p90 ~31), book feed 10 ms. Taker (FAK) orders are held
  by Polymarket: fast-value-live's took 207–2670 ms to post (median ~460) and most found the asks gone.
* Trader data: data-api timestamps are block time (~1 s after the match); `/trades?user=&takerOnly=true` lists
  only a wallet's taker fills (diff against activity = maker fills).
* Trader PnL = cash flows + final resolution; fees/rebates excluded.
* Coinbase allows ~10 BTC-USD `ticker` subscriptions per IP; on the server every bot + the recorder takes one. A refused
  subscription gets an error message and then silence: the engine (`market_maker._coinbase`) and the recorder switch to
  the `matches` channel (same trades; bid/ask rebuilt from the maker side). Diagnostics shows "connected (matches)".
* Daily loss limit (fast_player `max_daily_loss_frac` × bankroll, from the value at local midnight): once passed, no new
  entries until midnight (signal result `daily_loss`). The bot page shows a banner and a "Reset daily limit" button. Off in live mode (the paper
  ledger only drives the signal; `live_daily_loss_usd` guards the wallet).
* The CLOB ends an HTTP/2 connection after 10,000 requests (GOAWAY, last_stream_id 19999); the gateway keep-alive
  reconnects at once and counts it (`reconnects`) instead of logging an error.

## 10. How to add things
* **New bot strategy:** easiest = subclass `PaperBot` (see `ml_maker.py`): set `CONFIG` (a `BotConfig`
  subclass) and `STRATEGY`, override `_side_quotes()` (and `_maybe_take()` if needed). Otherwise write
  `backend/bots/strategies/<name>.py` with an engine class (`__init__(bot_id)`,
  `start/stop/snapshot`, `running`, `publish_state`, `_thread`; persist via the same DB tables) and a config
  dataclass with `from_dict/to_dict`. Register in `registry.STRATEGIES`. The UI settings form renders any
  config keys; add labels in `frontend/src/pages/bots/BotSettings.tsx:META`. Add `docs/bots/<strategy>/README.md`
  (copy a sibling) and a row in `docs/bots/README.md`.
* **Bot history:** creating a bot writes its first `bot_changes` entry; record rebuilds, audits and simulator
  changes with `registry.add_change(bot_id, text, kind)` so the bot page's "About this bot" stays a true log.
* **Bot going live:** route orders through `trading.gateway.gateway.place(req | {"origin": bot_id})` from the
  server process, or give the bot process its own `Gateway()` (it is self-contained). Set `bots.mode='live'`.
  fast_player does the latter (see its docstring). When state has `live`, the bot page shows only real-account data
  (live card, live P/L chart, live KPI strip, "Live account" tab, live column in Signals); the paper twin has its own tab.
* **New trader metric:** compute in `traders/analysis.py` (into `summary` or a table), expose via
  `routes/traders.py`, render in `pages/traders/sections.tsx`.
* **New table:** add to `db.SCHEMA`, document in §5.
* **New page:** `frontend/src/pages/<area>/`, route in `App.tsx`, nav link in `TopBar`.
* **UI styling:** use classes/tokens from `styles.css` only; chart colors from `charts.tsx:C` (validated
  dark palette, fixed order s1…s8; green/red reserved for Up/Down and gain/loss).

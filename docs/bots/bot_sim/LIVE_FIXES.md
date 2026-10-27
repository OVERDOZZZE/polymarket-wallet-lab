# imb-maker-live: fixes to do (handover, 2026-10-25 ~21:50Z)

For the next Claude Code session. Read `CLAUDE.md` and `STRUCTURE.md` first. All code below is on git branch
`imb-maker` (not merged into `main`); the Dublin server runs exactly that branch's files.

## Current state

* Bot `imb-maker-live` (strategy `imb_maker`, `backend/bots/strategies/imb_maker.py`) trades **real money** on the
  Dublin server: mode `live`, systemd unit `pmtool-bot@imb-maker-live`. Wallet ~$300 USDC (deposit wallet).
* Settings now: B 5, inv_cap 25, n_levels 13, min_price 0.10, max_price 0.99 (ignored), imb_join 1.01 (never joins
  the best bid: always back_ticks 2), spend_cap_usd 100, live_loss_limit_usd 140, live_merge on, live_paused off.
* `fast-value-live` (the other live bot) is stopped and its unit disabled on purpose: both on one wallet compete for
  USDC. Do not restart it.
* Results: settled live trading PnL about -$36 (+$4 rebates est.). Before the 21:35Z fixes live fills marked out
  -4.2 c/share at 60 s (paper twin +0.3 c); after them -0.5 c on only ~2 windows / 46 fills.
* Paper comparison bots running on the same windows: `imb-maker-300` (same sizing, no spend cap) and `imb-maker`.

The user has explicitly authorized live trading of this bot and asked for these fixes. You may restart its unit to
deploy (a clean stop cancels its resting orders; `runner.py` treats SIGTERM as a clean stop). Never print, log or
return anything from `.env.live` or the `POLY_API_*` values. Do not place orders yourself outside the bot.

## Fixes to do now (the user approved all three)

### 1. Quote only between 15c and 85c

Fills at <= 15c or >= 85c lost ~ -8.8 c/share (10 fills). `ladders()` in `imb_maker.py` already stops below
`c.min_price` (floor) but ignores `max_price`.
* In `ladders(c, bb, ba, d_up, d_dn, net, allow)`: skip (do not `break`) any level whose token price is above
  `c.max_price`. Up level px (thousandths): skip if `px > round(c.max_price * 1000)`. Down level px is in Up terms
  (Down price = 1000 - px): skip if `1000 - px > round(c.max_price * 1000)`. Keep `n_levels` counted from the top.
* With the defaults (min 0.10, max 0.99) the output must stay identical to the reference: the fuzz test in
  `scripts/check_imb_maker.py` compares against `docs/bots/bot_sim/strategy.py` with defaults and must still pass.
  Add a case with max_price 0.85 (e.g. bb 0.88: Up levels 0.88..0.86 skipped, ladder starts at 0.85).
* Then set on the bot: `min_price 0.15`, `max_price 0.85` (see "Apply settings").

### 2. Dedicated cancel lane

Cancels go through `self.gw.pool` (4 threads, `backend/trading/gateway.py`), shared with order posts, so a guard
cancel can wait behind posts. Most losses are fills that land while our cancel is in flight (fill received after
cancel sent: p50 ~285 ms).
* `Gateway.__init__`: add `self.cpool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trade-cancel")`.
* `imb_maker.py`: `_live_cancel` and `_live_shutdown` must run `gw.cancel_batch` on `gw.cpool`; posts, reconcile,
  sweep stay on `gw.pool`; merges stay on `gw.bg`. `_live_call(fn, *args)` currently always uses `self.gw.pool`:
  give it a `pool` argument (dry run path unchanged).
* `scripts/check_imb_maker.py`: `FakeGateway` needs `cpool` (can be the same executor).

### 3. Shorter ladders: 13 -> 7 levels per token

One taker order sweeps many of our levels at once (net reached -49/-54 against inv_cap 25). Fewer levels = smaller
sweeps, fewer orders/cancels, less rate-limit pressure. Settings change only: `n_levels 7`.

### Apply settings and deploy

1. Run `.venv\Scripts\python -m scripts.check_imb_maker` locally: must end with `all checks passed`.
2. Rebuild the UI only if you touched `frontend/` (`cd frontend && ..\.tools\node\npm.cmd run build`; under the
   Bash tool the esbuild-wasm build crashes *after* writing `dist/` when stdout is the tool's console: redirect its
   output to a file).
3. Commit on branch `imb-maker` (no Claude co-author line: user preference).
4. Before copying to the server, check the server's copies still match the previous commit (no drift), as done
   so far: `diff --strip-trailing-cr <(git show HEAD~1:<file>) <(ssh ... cat ~/app/<file>)`.
   Server: `ssh -i dublin-key-aws.pem ubuntu@108.130.59.134`, code in `~/app` (IP may change: see memory
   `dublin-server`). Copy only changed files (`scp` / `tar`), never `.env*`, `*.pem`, `data/`.
5. On the server: `.venv/bin/python -m scripts.check_imb_maker`, then set the config and restart:
   ```
   .venv/bin/python -c "from backend.bots import control, registry; control.save_config('imb-maker-live', {'min_price': 0.15, 'max_price': 0.85, 'n_levels': 7}); registry.add_change('imb-maker-live', 'Quote 15-85c only, 7 levels, dedicated cancel lane.', 'settings')"
   sudo systemctl restart pmtool-bot@imb-maker-live
   ```
   Do not restart `pmtool-server` unless needed: it disarms manual trading on the Trade page (tell the user if you do).
6. Verify after ~90 s from `data/bots/imb-maker-live/state.json` (`live` key): `state` None, `rej` without
   `error` / rate limits, `resting` > 0 while a window quotes, `errors` empty; `tail data/bots/imb-maker-live/bot.log`.
   Cross-check open orders on the exchange vs the bot (gateway `open_orders_in` / `client.list_open_orders()`).
7. Update `STRUCTURE.md` (gateway `cpool`, ladder max_price) and `docs/bots/bot_sim/README.md` (imb-maker-live
   version notes).

## Known open issues (not part of the approved fixes; report, don't change without asking)

* Latency doubled during the evening: post p50 ~90 ms / p90 ~500 ms, cancel p50 ~75 ms (first hour: 47 / 37 ms).
  Check whether it is Polymarket or us (`sign_ms` vs `post_ms` in `live_orders.data`, server CPU).
* `not_enough_balance` refusals (10 since 21:35Z): $300 is tight while winnings await redemption.
* Inventory cap overshoot from sweeps (fix 3 reduces it).
* `gateway.py` already contains a 2 s throttle of fill-triggered balance refreshes for the API server's gateway
  (rate-limit errors on `balance-allowance` in the UI). The bot processes have it; the `pmtool-server` process does
  not until it is restarted. Harmless (balance display only).
* Cosmetic: "RuntimeError: Event loop is closed" traceback in bot.log on restart (gateway `publish` from a bg thread
  after the loop closed). Could guard `publish` with `if self.loop.is_closed(): return`.

## How to judge it afterwards

Analysis scripts used so far (ad hoc, in /tmp on the server; rebuild them if gone): per window live vs paper twin
(`bot_windows` of `imb-maker-live`) vs `imb-maker-300`; live fill markout at 10 / 60 s from the window logs
`data/bots/imb-maker-live/windows/<day>/<slug>.json.gz` (`live.fills` rows vs mid from `decisions` rows); fills split
by whether their cancel was already sent and by ticks behind the best bid. Target: live markout >= 0 c/share before
rebates over >= 20 windows. Live loss limit $140 halts quoting automatically; kill switch = Settings -> Live
(`live_paused`).

# Imbalance maker in London: plan (2026-11-01)

Goal: find out whether the imbalance maker (`imb_maker`) becomes profitable now that the account may trade from AWS
London (eu-west-2), and if so run it live, small. Work through the phases in order; each phase ends with a short
written result (append to the "Log" at the bottom) before the next starts.

## Read first

* `CLAUDE.md`, `STRUCTURE.md`.
* Memory notes (`~/.claude/projects/c--Users-faust-Desktop-Projects-Polymarket-BTC-Analysis/memory/`):
  `london-colocation-approved`, `order-heartbeat-cancels`, `imb-maker-paper`, `dublin-server`, `latency-profile`,
  `polymarket-rules-research`.
* `docs/bots/imb_maker/LIVE_POSTMORTEM_2026-10-27.md`: why the live run lost (stale-quote fills when the book moved;
  cancels 56 ms calm / 112 ms during moves; if cancels had landed within ~35 ms the day would have been ~+$25 instead
  of -$11).
* `docs/polymarket/` (rules, rate limits, taker delay, co-location), `docs/latency/README.md`.
* Code: `backend/bots/strategies/imb_maker.py` (strategy + live layer), `backend/trading/gateway.py`,
  `backend/bots/replay.py`, `backend/bots/latency.py` + `latency_profile.json`.

## Rules

* Live orders, arming, live cancels: only when the user says so in that session (CLAUDE.md). Tiny sizes only.
* Never print or persist anything from `.env.live` or `POLY_API_*`.
* No live orders from Dublin any more (Ireland is ambiguous in Polymarket's rules); London only.
* Keep order and cancel rates moderate (our ~6k orders/hour bursts may have caused the account flag).
* Server is the source of truth; keep the local branch in sync with what is deployed.

## Status 2026-11-01 (before Phase 1)

* Polymarket approved co-location and whitelisted the wallet on 2026-10-30 (memory `london-colocation-approved`).
  The account-wide ~16 s cancels are not yet re-tested.
* Dublin server (i-094f7c59e17b8b2c6, eu-west-1b, 30 GB disk): recordings deleted on 11-01 after the disk filled up
  (local copies of 10-24..10-27 15:00 are in `data/recordings_server/`). The user archived every late-fav bot and
  disabled the recorder. Its UI is current (bot cards fixed 11-01).
* The auto-mode classifier blocks Claude from stopping, archiving or restarting running bots and from live arming on
  the server: prepare such commands for the user to run.

## Phase 1: London server

1. Give the user exact AWS console steps: EC2 in eu-west-2, same instance type as Dublin (c7i.xlarge), Ubuntu 24.04,
   **100 GB gp3** root disk (the recorder writes ~6 GB/day), a key pair (key pairs are per region: create one or import
   the existing public key), security group with SSH from the user's IP only. No Elastic IP (the AWS org's policy
   denied it in Dublin). One instance per AZ for a short test is fine (zone ids: 2a=euw2-az2, 2b=euw2-az3,
   2c=euw2-az1); if AWS CLI credentials exist on this PC, script it instead.
2. Measure from each AZ: TCP/TLS to clob.polymarket.com, kept-alive HTTPS RTT, market websocket feed lag
   (book message exchange timestamp vs receive time). Keep the fastest AZ, terminate the others.
3. Install the app there: copy `~/app` from Dublin (code, `data/btc5m.db`, `data/bots`, `data/cache`; not old
   recordings), run `deploy/install.sh`. The user copies `.env` / `.env.live` if the classifier blocks it. Check
   `polymarket.com/api/geoblock` from the new IP. Start the server UI and the recorder (with log rotation or a
   retention limit so the disk cannot fill). Keep the paper bots stopped until Phase 3.
4. Update the `dublin-server` memory note and STRUCTURE.md / deploy notes with the new host. Keep Dublin running
   until London is verified, then hand the user the command to stop it.

## Phase 2: measure live behaviour (needs the user's go; a few dollars)

1. Resting test: 2-3 post-only bids far from the price (cannot fill), left 60 s. Do they survive past 16 s?
   (the account-wide cancels in `order-heartbeat-cancels`). If not, stop and tell the user to ask
   certs@polymarket.com, with the order ids.
2. Speed test: ~20 rounds of post-only place + cancel, as in `scripts/order_speed_probe.py` / `delay_probe.py`:
   send -> ack, send -> visible on the book, cancel send -> gone from the book. Compare with Dublin
   (post-only ack p50 65 ms, cancel p50 61 ms).
3. Add a `london` site to the latency profile (`scripts/latency_build.py`), so replays can use it.

## Phase 3: decide on paper before risking money

1. Replay `imb-maker-10`'s config on the server recordings with the London latencies (`backend/bots/replay.py`,
   pessimistic queue / trade-through variants as in the post-mortem), plus a few variants that attack the known loss
   causes: pull quotes when the queue ahead of us thins or the best bid moves through us, wider back-off, smaller
   size, quote only when the guard feeds are fresh.
2. Re-score the 10-27 live day with London cancel times (the post-mortem's "unwanted for > X ms" counterfactual).
3. Go / no-go: the variant must be positive under the pessimistic fill model and London latency in at least two
   separate periods, with markout at 1 s >= 0. If none is, write that down and stop here.

## Phase 4: small live run (needs the user's go)

1. Run the best variant as a new live bot in London with hard limits (e.g. $20 wallet share, loss limit $10, small
   size), order heartbeats on, and its paper twin alongside.
2. After every ~10 windows compare live vs twin (fills, markout, PnL) the way the post-mortem did.
3. Report to the user; scale only with their decision.

## Log

(append dated results per phase here)

### 2026-11-01 Phase 1: London server

* AWS CLI installed on the home PC (`aws login`, short-lived console credentials; role AccountFullAccessRole).
  London's on-demand vCPU quota is **5**: one c7i.xlarge fits, so the AZs were tested on c7i.large boxes, two at a
  time (eu-west-2d = euw2-az4 also exists; not tested, Polymarket named only 2a/2b/2c).
* AZ test (`deploy/az_probe.sh` = `scripts/latency_probe.py`, 10 min each, Dublin probed alongside; raw files
  `data/latency/probe-{london-euw2-az*,dublin}-20261101-*.json`), p50 ms:

  | site | CLOB kept-alive HTTPS | book ws ping | book feed age |
  |---|---|---|---|
  | Dublin (2 runs) | 21.8 / 22.0 | 12.9 / 12.0 | 9.3 / 9.6 |
  | euw2-az2 (2a) | 16.1 | 3.2 | 4.9 |
  | euw2-az3 (2b), 2 runs | 16.7 / 16.1 | 5.8 / 3.3 | 6.7 / 5.2 |
  | euw2-az1 (2c) | 14.4 | 4.7 | 5.2 |

  Head-to-head az1 vs az2 at the same time (CLOB `GET /time`, kept alive): run 1 az2 15.4 vs az1 18.4 (one
  connection each); run 2 with 6 fresh connections each: az1 14.9 vs az2 16.3. **Each connection's p50 ranges
  14-18.5 ms in both AZs** (Cloudflare LHR edge, ~1 ms TCP; the spread is the backend a connection lands on),
  larger than the AZ difference. Chose **eu-west-2c (euw2-az1)**: best in 2 of 3 same-time runs, lowest min
  (12.9 ms). Idea for later: open a few CLOB connections and keep the fastest (~2-4 ms).
* Server `pmtool-london`: i-02cd06aeb8754c488, eu-west-2c, c7i.xlarge, 100 GB gp3 (DeleteOnTermination=false),
  Ubuntu 24.04, 4 GB swap (swappiness 10), IP 18.169.243.198, key pair `pmtool-key` (= dublin-key-aws.pem), SG
  `pmtool-ssh` (22 from 24.79.50.8 only). Test boxes terminated.
* App copied from Dublin's `~/app` (code, `data/` without recordings; `btc5m.db` via the SQLite backup API,
  integrity ok, 26 bots) plus the newer local files (latency profile/scripts, docs, recorder free-space guard).
  `deploy/install.sh` with no bot ids: pmtool-server (UI 200) and pmtool-recorder running, no bot units.
  Recorder: 7-day retention, plus `MIN_FREE_GB = 10` guard (deletes oldest days sooner).
* `polymarket.com/api/geoblock` from 18.169.243.198: `blocked: true, country GB` (IP-based; the whitelisting is
  per wallet, so the Phase 2 resting order is the real test).
* Recorder "1013 slow consumer" disconnects on 15m book sockets also happen on Dublin (462 in its log): known, not
  London-specific.
* Follow-up: the user copied `.env` / `.env.live` to London (both 600). Dublin (i-094f7c59e17b8b2c6) is still
  running as of 08:50Z; stop it with `aws ec2 stop-instances --region eu-west-1 --instance-ids i-094f7c59e17b8b2c6`.

### 2026-11-01 Phase 2: live behaviour from London (user's go)

* **Resting test** (`scripts/resting_probe.py`, 08:33Z): 3 post-only BUY 5 Up @ 0.01/0.02/0.03 on a window not yet
  open. Posts 26-52 ms, all `LIVE`; **all three survived 60 s** and were cancelled by us (29 ms); none left open.
  Closed-only mode False. The ~16 s account-wide cancels of 10-27 are gone (whitelisting / London).
* **Speed test** (`scripts/order_speed_probe.py 20`, 60 unfillable orders, 08:34Z), London vs Dublin:

  | | London p50 / p90 ms | Dublin p50 / p90 ms |
  |---|---|---|
  | post-only sent → answer (seq + burst5) | 39 / 48 | 65 / 177 |
  | cancel sent → answer | 30 / 33 | 61 / 156 |
  | sent → on the book (exchange clock) | 27 / 34 | - |
  | cancel sent → off the book (exchange clock) | **20 / 23** (max 24) | - |
  | book feed (receive - exchange time) | 4 / 5 | 12 |

  Pipelined (cancel right behind the post): 19 of 20 posts were refused "already canceled": the cancel overtook
  its post, so an in-flight cancel is honoured and the order never rests. The probe's book matching was wrong
  (counted refused orders; summary p50 ~1.1 s): fixed to match accepted orders only and split multi-order steps.
* **Latency profile**: site `london` added (15-min `latency_probe --site london` at 08:34Z + the order run via
  `order_speed_probe --site london` -> `data/latency/orders-london-*.json`, read by `latency_build.py`). Dublin
  and shared keys unchanged. London has no `bot_feed.*` yet (needs `latency_history.py` on a day of London
  recordings). Feed medians London / Dublin: Polymarket book 4 / 12, Kraken 13 / 34, Coinbase 42 / 49, Binance
  futures 127 / 106 (worse), Chainlink 255 / 282. Docs: `docs/latency/README.md` "London site".
* Meaning for Phase 3: the post-mortem's counterfactual needed cancels effective within ~35 ms of the decision;
  calm London cancels are off the book in 20 ms (p90 23). Unknown: whether cancels still slow down during BTC
  moves (Dublin's p50 was 112 ms on the fills that mattered) - that is exchange congestion, not distance.

### 2026-11-01 Phase 3: paper decision (one day, user asked for speed)

`scripts/imb_london_replay.py` (new; nothing in the strategy changed): imb-maker-10's exact config (B 5, 1 level,
cap 6, $6 hard spend cap, 0.30-0.70) replayed on the Dublin recordings of **2026-10-25** (local
`data/recordings_server`), 12 x 2 h slices in parallel (34 min on 12 cores), periods p0 = 00-12Z, p1 = 12-24Z.
London timing = recorded receive times shifted to London's feeds (Polymarket book -8 ms, Binance futures +21 ms).
Latency decision -> on book / cancel effective: calm 30/23 ms (Phase 2), stress 30/60 (cancels ~3x slower during
moves, as Dublin's were), Dublin reference 60/90 (post-mortem). Fill models: queue (paper), **pq** (shares joining
our level within 60 ms of our decision go ahead of us), **tt** (fills only on prints through our price). mk1 / mk10
= share-weighted markout at 1 s / 10 s, cents. Raw: `data/replays/imb_london-2026-10-25_0000-2026-10-26_0000.json`.

| variant | all: fills / net $ / c/sh / mk1 | p0 net / mk1 | p1 net / mk1 | passes rule? |
|---|---|---|---|---|
| queue, calm (paper ref) | 314 / +35.17 / +2.55 / +0.54 | +19.59 / +0.28 | +15.58 / +0.71 | (not pessimistic) |
| **pq, calm** | 224 / +25.63 / +2.52 / +0.20 | +20.55 / +0.29 | +5.08 / +0.14 | **yes** |
| tt, calm | 172 / +6.97 / +0.90 / -2.07 | +7.65 / -1.40 | -0.67 / -2.56 | no |
| pq, stress | 208 / +18.09 / +1.94 / -0.34 | +7.58 / -1.60 | +10.51 / +0.12 | no (p0 mk1) |
| tt, stress | 162 / -33.47 / -4.69 / -1.71 | -23.15 / -3.15 | -10.33 / -1.09 | no |
| tt, Dublin 60/90 as recorded (ref) | 163 / -19.43 / -2.64 / -1.73 | -10.06 / -2.04 | -9.36 / -1.63 | no |
| back_ticks 3, pq calm | 184 / +0.72 / +0.09 / +0.31 | -5.80 / -0.04 | +6.52 / +0.48 | no |
| imb_join 0.6, pq calm | 228 / +22.27 / +2.18 / -0.49 | +25.98 / -0.99 | -3.71 / -0.05 | no |
| **guard 0.3 bps / pull 2 s, pq calm** | 238 / +11.85 / +1.08 / +0.44 | +8.04 / +0.60 | +3.81 / +0.37 | **yes** |
| band 0.40-0.60, pq calm | 223 / +37.45 / +3.83 / -0.29 | +34.01 / -0.79 | +3.43 / +0.04 | no (p0 mk1) |

Reading (rebates ~+$2.5-3.5/day included in net; settlement PnL per window is a coin flip, so mk1 is the signal):
* Sanity: the Dublin reference (tt, 60/90) loses with mk1 -1.7 c, live's signature on 10-27 (-1.98) and the
  post-mortem's sim (-1.22): the replay still reproduces the known-bad case.
* London speed moves the result from clearly negative to **positive only under the pq model with calm cancels**
  (base config and the stronger guard pass both periods with mk1 >= 0). Under trade-through fills (the model that
  matched live's markout in the post-mortem) every London variant has mk1 -1.4 to -2.6 c, and with stressed
  cancels (60 ms) pq's p0 markout turns negative too.
* So the answer hinges on one unmeasured number: **how fast a London cancel takes effect while the book is
  moving.** At ~23 ms the strategy is plausibly positive (~+1-2.5 c/share); at ~60 ms it is not.
* Go / no-go by the rule above: **conditional go** for `guard03` (spike_bps 0.3, pull_ms 2000; best markout in
  both periods) and the base config, *only* if congested London cancels stay near the calm 20-25 ms. Not tested
  for speed: pulling when the queue ahead thins, wider back-off beyond 3 ticks, imb-maker-300's ladder; one day only.
* Cheapest next step before risking money: measure cancel latency **during BTC moves** with unfillable orders
  (rest bids far from the price on the active window; on each Binance spike, cancel and time ack + off-book).
  Then Phase 4 with guard03 only if the congested cancel p50 is <= ~30 ms.

## Overnight mandate 2026-11-01 ~09:50Z (user asleep; full authority, $10 of live money)

User: run the cancel stress test; if positive go live (required); else try other variants / strategies / speed
work; keep working until a profitable bot is live. Rules Claude follows:
* Live losses never exceed **$10 in total**, whatever the wallet holds; every live bot has its own loss limit
  inside that. Live orders only from London, BTC Up/Down only, average < 1 order/s, no approval changes, transfers
  or withdrawals. Never read / print `.env.live` or `POLY_API_*`.
* A live bot is stopped at its loss limit, or when its 1 s markout is negative after ~100 fills, or when fills after
  our own cancel resemble 10-27. A losing bot is not kept live just to have one live.
* Never idle: always a test / replay / bot running plus a background check that wakes Claude. Log every result here
  with UTC times.
* Order of work: cancel stress probe -> replay check on a second day with the measured cancel times -> dry run ->
  live small with a paper twin -> compare every ~10 windows -> otherwise other variants, speed work, other strategies.
* 09:59Z: the user deposited $10; wallet USDC = $10.77 (0 positions). Live budget = $10 of losses at most.
  Dublin instance stopped (~09:56Z). late-fav v3 ruled out: paper 487 windows -$1,446 (6 full-stake losses).

### 2026-11-01 Cancel stress probe (09:52-10:52Z, `scripts/cancel_stress_probe.py`, London)

312 unfillable post-only bids (0.01, active 5m window), 311 cancelled by us, 0 fills, 1 failed post. Cancel sent ->
off the book (exchange clock), ms:

| cancel | n | p10 | p50 | p90 | max |
|---|---|---|---|---|---|
| calm (every ~12 s, no spike for 3 s) | 246 | 14 | **15** | 118 | 1,514 |
| **at a Binance futures spike (>= 0.5 bps / 1 s)** | 60 | 37 | **103** | **815** | 1,067 |

By book activity in the 500 ms after the send (ack ms): < 30 msgs p50 24 / p90 35 (n 209); 30-100 p50 46 / p90 456;
>= 100 p50 113 / p90 226. **During moves London's cancels are as slow as Dublin's (median ~100 ms vs 112): the
queue is at Polymarket, not in the network.** Rule set before the test (congested cancel p50 <= ~30 ms): **not met**
-> imb_maker as built is no-go for live; the 10-24 replay with a 100 ms / 1000 ms spike cancel decides whether any
variant survives this regime. Raw: `data/probe/stress-20261101-095208.json`.

### 2026-11-01 ~11:25Z Replay day 2 (2026-10-24) with spike-time cancel latency

(10-26 crashed: its 400 MB hourly files x 12 processes ran out of memory; 10-24 used instead.) `-spikeN` = cancels
take N ms while a guard spike is active (pull window), 23 ms otherwise.

| variant (10-24) | net $ / mk1 c | p0 net / mk1 | p1 net / mk1 |
|---|---|---|---|
| pq calm | +58.25 / +0.59 | +5.21 / +0.11 | +53.05 / +0.80 |
| tt calm | +29.71 / +0.78 | +5.22 / +0.27 | +24.49 / +0.96 |
| pq stress (60 ms always) | -21.64 / -0.25 | -15.73 / -1.18 | -5.91 / +0.23 |
| tt Dublin 60/90 ref | -2.71 / -1.89 | +14.25 / -2.05 | -16.96 / -1.78 |
| guard03 pq calm | +48.96 / +0.71 | +1.60 / +0.13 | +47.36 / +0.93 |
| **pq spike100** | **+65.32 / +0.77** | +15.77 / +0.29 | +49.55 / +0.99 |
| guard03 pq spike100 | +44.25 / +0.96 | -7.76 / +0.21 | +52.01 / +1.16 |
| pq spike1000 | +0.29 / **-2.36** | -8.53 / -4.36 | +8.82 / -1.51 |
| guard03 pq spike1000 | -41.14 / -2.48 | -1.21 / -3.58 | -39.94 / -1.83 |

The measured spike-cancel median (~100 ms) is survivable; a 1 s tail is not, and the measured p90 is 815 ms.
Calm-time cancel speed matters too (60 ms on every cancel loses). Next: draw each cancel's latency from the
measured stress-probe distributions (calm 246 / spike 60 samples; spike = a 0.5 bps Binance move within 1 s of the
cancel), `-emp` variants, on both days. Also tested on London recordings (3 h): a delta-aware "fair-value" guard warns
of 27% of Polymarket 2 c moves at 2.5 pulls/min vs 22% at 2.8/min for the fixed 0.5 bps guard; ~70% of moves have no
BTC precursor in any feed (Coinbase / Kraken add nothing over Binance).

### 2026-11-01 12:11Z Deciding replay: measured cancel latency per cancel (`-emp`), 2 days

Each cancel's latency is drawn from the stress probe's off-the-book distribution (calm 246 / spike 60 samples,
spike = a 0.5 bps / 1 s Binance move within 1 s before the cancel); orders live 30 ms after the decision.
2026-10-24 + 10-25, p0..p3 = 12 h each. `fair1` = FairGuard (delta-aware, 1 c, pull 2 s), now in imb_maker.py as
`guard_mode="fair"`.

| variant | all: fills / net $ / mk1 | p0 | p1 | p2 | p3 | periods net>0 & mk1>=0 |
|---|---|---|---|---|---|---|
| pq emp | 709 / +43.82 / +0.52 | -2.13 / +0.13 | +48.93 / +0.89 | -3.55 / +0.12 | +0.56 / -0.02 | 1 |
| tt emp | 431 / +17.52 / -0.61 | +0.84 / -2.43 | +7.74 / +1.17 | +12.37 / -2.01 | -3.44 / -0.86 | 1 |
| guard03 pq emp | 746 / +50.95 / +0.75 | -1.53 / -0.02 | +47.98 / +1.36 | +0.62 / -0.48 | +3.88 / +0.63 | 2 |
| **fair1 pq emp** | 689 / **+47.35 / +0.88** | **+4.32 / +0.07** | **+37.61 / +1.49** | +5.84 / -0.41 | -0.42 / +0.83 | **2** (net>0 in 3) |
| fair05 pq emp | 646 / +35.33 / +0.73 | -20.38 / -0.05 | +40.89 / +1.24 | +10.49 / -0.32 | +4.33 / +0.67 | 2 |
| queue emp (paper) | 873 / +48.82 / +0.63 | +2.25 / +0.02 | +35.83 / +1.03 | +20.07 / -0.02 | -9.33 / +0.65 | 2 |

**Go (rule met under the pq model) with `fair1`.** Caveat kept in view: under trade-through fills the markout is
negative (-0.61), and in the post-mortem live did worse than even tt. The live run is the test; the $8 loss limit
bounds it. Profit concentrates in one half-day (10-24 12-24Z), so expect long flat stretches.

### 2026-11-01 Phase 4: imb-london LIVE

* Code: `imb_maker.py` gained `guard_mode="fair"` (FairGuard) and order heartbeats (`live_heartbeat_s` 5, late_fav's
  loop); `scripts/check_imb_maker.py` all checks pass. `scripts/imb_london_live.py` creates the bot.
* 12:12Z bot `imb-london` created (fair1: guard_mode fair, fair_thr 0.01, pull 2 s; imb-maker-10's size B 5, 1 level,
  inv_cap 6, $6 hard spend cap / window, band 0.30-0.70; bankroll $10; live loss limit $8), dry run 12:12-12:24Z:
  446 simulated orders, all accepted / cancelled, 0.79 orders/s average, guard ~4 pulls/min, no errors.
* **12:24Z armed: mode live** (systemd pmtool-bot@imb-london). Wallet $10.77, heartbeats ok. The paper twin runs in
  the same process. Stop rules: loss limit $8 (automatic); stop by hand if 1 s markout < 0 after ~100 fills or if
  fills after our own cancel look like 10-27.
* First live windows: 12:25 window bought Up 6 @0.69 + Down 5 @0.54 + 5 @0.60 ($9.84), 6 pairs merged, Up won:
  **-$3.84** (twin -$2.28). 12:30 window Down 6 @0.70, Down won: **+$1.80**. Realized -$2.04 after 2 windows;
  twin -$3.41 after 4. The bot halted itself once (worst case of the two open windows > the $8 room) and resumed
  after resolution, as designed. Winners are redeemed / pairs merged automatically (USDC back to $8.73).
* Order path in the bot: post p50 55-74 ms, cancel 45-53 ms before the restart; 35 / 28 ms just after it.
* 12:40Z variance cut (the $8 budget allows ~2 bad windows at a ~$4 swing vs a ~+$0.20/window expected edge):
  band 0.34-0.66 (every order exactly 5 shares, so one fill per side flattens), spend cap $3 hard, inv_cap 5.
  Worst case per window ~ one unpaired fill (<= $3.30). Restarted; ledger rebuilt from live_orders.
* 12:51Z `fill_report imb-london --since 12:24`: LIVE 9 fills / 47 sh: edge after 5 s **-3.0 c/sh**, 30 s **-5.65**, 60 s
  -12.0; the 2 fills with a guard pull within 2 s: -35 c/sh. PAPER twin 8 fills: +5.4 / +3.3 / -4.3. **Live vs twin
  after 30 s: -8.9 c/share** - the 10-27 signature again (quotes already being pulled get hit). Realized -$4.34 after
  3 windows, $1.81 of loss room left.
* **12:53Z stopped live** (disarmed to paper, restarted; 0 open orders). Verdict: London speed does not fix the
  maker; even the replay with measured per-cancel latency is too optimistic about live fills. imb-london keeps
  running in paper mode. Money left: USDC $1.78 + the last window's shares (cost $6.85) pending settlement.
* ~13:15Z wallet USDC $6.78 after the last window was paid out: **the live test cost $3.99** of the $10
  ($10.77 -> $6.78). No open orders; no open BTC positions of value.
* Side checks while stopping: (a) pre-window quoting (fair 0.50 before the start?) - no: the strike is the PREVIOUS
  window's settlement TWAP, so the next window's fair moves during the last minute before its start (trades at
  0.29 / 0.71 seen 19 s before a start); earlier than ~2 min the books are already 0.49 / 0.51 with ~500-share queues
  and only ~10-15% of pre-start volume trades that early. (b) late-fav v3's 6 paper losses were real last-minute
  rallies ($8 through the strike), not TWAP-estimation errors: arming 120 s out is a forecast, not a lock.

## Next after the maker (user request 2026-11-01 ~13:20Z)

When the imb maker is finished (live and final, or closed as no-go): build a **fast_player live variant at the
highest possible speed from London** (fast order path, London feeds) and run a small live test to see whether its
**taker fill rate** rises after the move (Dublin: FAKs filled ~7.7%; Polymarket's 150 ms taker delay still applies).
Paper first: fast_player with London's measured delays (latency_profile site `london`, `fak_match_ms` hold model)
and other realistic factors. The maker stays the priority until then.

### 2026-11-01 13:01Z Fill-model validation on 10-27 (Dublin, the live day) - maker CLOSED as no-go

Replay of imb-maker-10's config on 10-27 00:40-08:10 as recorded in Dublin, each cancel drawn from the measured
distribution + 8 ms (`-empd`), vs the real live run:

| 10-27 | fills | net $ | c/share | mk1 c |
|---|---|---|---|---|
| **live (real)** | 258 | **-10.74** | **-0.91** | **-1.98** |
| replay pq + measured latency | 169 | +20.64 | +2.80 | -0.18 |
| replay tt + measured latency | 101 | +2.27 | +0.50 | -0.28 |
| replay paper queue + measured latency | 232 | +24.36 | +2.25 | +0.90 |

Even with real cancel timing the replay is ~3.7 c/share and ~1.8 c of 1 s markout too optimistic (it misses where
live sits in the queue). Applied to the London prediction (fair1: +1.49 c/sh, mk1 +0.88) that is ~-2 c/share live,
which today's 9 London live fills confirm (-8.9 c/sh vs the twin). **Verdict: the imbalance maker is a no-go from
London too. Closed.** imb-london stays in paper mode only. Next: fast_player (see "Next after the maker").

## 2026-11-01 Fast player London fill-rate test (user's request)

* Dublin reference, `fast-value-live` (10-23..10-26): 641 live FAK buys, **64 filled (10.0%)**; sign p50 2 ms, post
  p10 / p50 / p90 292 / 655 / 2,058 ms (the exchange holds marketable orders; our own overhead is ~2 ms).
* `scripts/fast_london_live.py`: bot **fast-london** = fast-value-live's signal unchanged (Coinbase + Kraken + Binance
  spot spike 2 bps / 1 s, value entries <= 0.60, 2 per window, hold), so its fill rate is comparable; live orders $1
  each, live loss limit $5 (the remaining overnight budget is $6.01); paper twin on the queue model (queue_max_ms
  2200). London's own speed gain: Kraken 13 ms vs 34, Coinbase 42 vs 49, ~6 ms less to the CLOB.
* 13:03Z **armed live** (wallet $6.78). `fast-value-live` switched to paper in the London DB (shared wallet).
* Paper prediction (`scripts/fast_london_replay.py`, fast-value-live's config, 10-24 + 10-25, recordings as in Dublin
  vs re-timed to London: Kraken -20 ms, Coinbase -7, Binance spot +2, futures +21, book -8, Chainlink -27):

  | model | orders | filled | fill % | net $ | ROI |
  |---|---|---|---|---|---|
  | queue (queue_max_ms 2200), Dublin | 122 | 10 | 8.2 | +1.36 | +0.8% |
  | **queue, London** | 122 | 10 | **8.2** | +2.26 | +1.3% |
  | 50 ms paper, Dublin | 120 | 116 | 96.7 | +1,881 | +81% |
  | 50 ms paper, London | 120 | 116 | 96.7 | +1,924 | +83% |

  Prediction: **the fill rate does not change** - the ~20 ms earlier signal is small next to the exchange's hold,
  during which the asks are pulled; on the realistic model the strategy is about break-even. The live test measures
  it for real. First live order 13:1xZ: missed, answered in 620 ms (Dublin p50 655).

### 2026-11-01 ~13:30Z Bug found in the `-emp` replays (affects the 12:11Z "deciding replay")

The engine drops a cancelled order at `cancel_ms + _cancel_lat()` (market_maker.py ~1187 / 1215), and the `-emp`
variants reported 30 ms there while drawing per-order latencies elsewhere. So every `-emp` cancel effectively took
**at most ~30 ms**: the measured spike tail (p50 103 / p90 815 ms) was never applied. The 12:11Z table that led to
the go (fair1 +$47, mk1 +0.88) and the 13:01Z `-empd` validation were therefore calm-latency runs. The fixed-value
`-spike100` / `-spike1000` variants were not affected (spike1000 was clearly negative). Fixed in
`scripts/imb_london_replay.py` (nominal 3,000 ms; each order pruned at its own draw on the 50 ms tick); calibration
and the London comparison are being re-run. The live verdict (stopped, -$3.99) stands on its own live evidence.
* Re-run with the fix, validation on 10-27 (Dublin, live day): replay now **reproduces live** - tt + measured
  latency 308 fills / -1.62 c/sh / mk1 -1.82; pq 299 / -1.22 / -0.86; pqb (cancels never move us up) 318 / -1.62 /
  -1.44; live 258 / -0.91 / -1.98. The slow-cancel tail during busy books explains the live losses.
* Re-run on London 10-24 + 10-25 (fixed): **every variant has negative 1 s markout**: pq -0.90, tt -1.70, pqb -1.23,
  fair1 pq -0.20 / pqb -0.81, guard03 pq -0.24 / pqb -0.70 c (pqb and tt negative in all four half-days). Settlement
  net swings with luck (+$48 .. -$33). **The maker is a no-go in London on a validated model.**
* Last maker idea being tested: an **activity gate** - pull everything when the market's book has >= N messages in
  500 ms (cancels into a quiet book: ack p50 24 / p90 35 ms; into a busy one p50 113), with the replay's cancel
  latency drawn by the same activity bucket (`-empa`, validated on 10-27 first).
* 15:46Z activity gate result. Validation on 10-27 with latency drawn by book activity (`-empa-dub`): pqb 349 fills /
  +0.31 c/sh / mk1 -1.07, tt 343 / +0.05 / -1.39 (live -0.91 / -1.98: right direction, still a bit optimistic).
  London 10-24 + 10-25: no gate pqb mk1 -0.89, tt -1.50; **gate 30 msgs pqb -1.56, gate 60 pqb -1.43, gate 30 tt
  -1.85** (72-102 windows, 90-136 fills). Staying out of busy books leaves mainly the fills that arrive without
  warning, and those are the worst. **Maker closed for good: every variant on every validated model has negative
  1 s markout in London.**
* 16:02Z fast player variants (queue model, London timing, 10-24 + 10-25, `fast_london_replay.py --set 2`):
  extra_pay 2 c: 19 / 122 filled (15.6%), +$107 (+41%); pay 3 c: 24 (19.7%), +$182 (+50%); + Binance futures: 8 (6.6%),
  -$42; futures + pay 2 c: 13 (10.7%), +$104; cap 0.70: 13 (8.7%), +$17. **Not a go:** 19-24 fills (each a settlement
  coin flip of ~+/-$10), and these days are in-sample for the 10-26 allowance study, whose out-of-sample period
  (old 64 h) lost with the same allowance. To verify on fresh London recordings (from 11-01 08:19Z on).
* fast-london live by 15:46Z: 12 FAKs, **1 filled (8%)**, that window lost the $1 (twin lost it too). Too few to
  compare with Dublin's 10%. Live budget used tonight: $3.99 (maker) + $1.00 = $4.99 of $10.
* 16:08Z out-of-sample check on London's own recordings (11-01 08:20-16:00, `--set 3`, queue model, no shift): base
  20 orders / 1 filled / -$20; pay 2 c 1 / -$20; pay 3 c 2 / -$40; futures + pay 2 c 2 / -$30 - every simulated fill
  lost. With the old out-of-sample loss, the in-sample +40-50% does not replicate: **no fast-player variant is a go.**
  fast-london keeps running live at $1 per order only as the fill-rate measurement the user asked for (room $4).

- 2026-11-01 18:37Z: user asked to run the paper imb-london (fair1) config live. New bot `imb-london-live` = imb-london's
  config exactly (fair guard 1 c / 2 s, band 0.34-0.66, B 5, inv_cap 5, $3 hard spend cap per window), mode live, live
  loss limit $5 (wallet $5.71, shared with fast-london's $1 FAKs). imb-london stays in paper as the twin. Fresh bot id so
  the old imb-maker-10 / imb-london live history doesn't count against the limit. Monitor: imb_london_status imb-london-live.

### 2026-11-01 ~20-23Z Trader forensics and the queue-priority ladder - ladder ruled out

* `scripts/trader_forensics.py`: every BTC fill of crouch / machinelearned / hcrystallash / wzbbb and ours (data-api,
  timed by tx hash against the recorded books), 10-25..27 + 11-01. Maker mk1 / settle c/sh: crouch +0.21 / +0.74,
  machinelearned +0.31 / +1.00, wzbbb +0.82 / +0.72; **us -1.86 / -2.89** (631 fills). Only ~5% of anyone's maker
  shares fill after a >= 1 bp BTC move against them; our calm fills still -1.5. 75% of our fills sweep the level
  (vs 23-41%); kept fills earn ~+1 c for everyone, swept lose. Our fills are worse in every bucket (depth, time in
  window, price, spread). Their takers are not faster than us (lag p50 ~0.9 s vs 1.1 s).
* Hypothesis: queue priority (resting ladder, Crouch shape) would give us their fill mix. Replay (`--set ladder`,
  13 levels B 5, $100 budget, London timing): 10-25 pqb mk1 -1.10 / -0.92 c/sh, pq -0.71 / -0.86, paper queue
  model -0.49 / -0.74, tt -1.43; 10-27 pqb -1.06 / -0.82, tt -1.49. 6 levels, always-join, guard 0.3: same; no guard
  -2.3..-2.5. **Negative on every fill model, including the optimistic paper one**: a Crouch-shaped ladder at our
  size does not get Crouch's fills. A bigger budget only scales the loss.
* Crouch reconstruction (10-25..27 fills): profit = **pairs**. 82% of Crouch's 5m shares pair up (Up + Down in the
  same window) at an average **0.919** -> +$42k over 3 days; the unpaired rest (mostly the losing side) -$33k; net
  +$9.5k. machinelearned the same (0.895). Ours: pairs at **1.023**. Pair cost falls with the window's swing (0.97 in
  calm windows, 0.86 in the widest). Replay of BOT_SPEC at Crouch's own size (B 60, inv_cap 300), 10-25 12-16Z:
  right volume, but +2.3..2.7 c per fill vs Crouch in the same (window, side, 15 s) cells -> pairs 1.02-1.04.
  Ladder 1-3 ticks deeper: gap stays 2-2.9 c. Quote to end-5 s: no. Strict "never above the side's window low +
  0..5 ticks": pairs 0.72 but only 26-41% paired, -8..-10 c/sh (overshoots). Crouch's fills sit on average 3.2 c below
  the replay's BTC fair value (ours ~0): they likely bid off a fair-value model; ours is too poorly calibrated
  (underconfident at 0.6-0.7) to copy that yet. Next: a calibrated fair model, then "bid <= fair - edge" ladders.
* Fair-value check (`scripts/fair_calib.py`, 15 samples per 5m window, 806 Dublin windows 10-24..27 + 128 London
  11-01): our TWAP random-walk model (strike = our Chainlink TWAP, spot = futures + basis, realized vol) is **worse
  than the Polymarket mid** (log-loss 0.519 vs 0.507; 11-01 0.533 vs 0.480); a logistic blend gives the model ~0
  weight (0.506). Inputs are right (side of strike matches the outcome 122/128 at 210 s). Trading the model's side
  when it disagrees with the mid by >= 5 c: +4.9 / -1.0 / -1.3 / +4.8 / -2.1 c per share on the five days - noise.
  **A fair value from public BTC feeds does not beat the market price**, so "quote off fair value" is no edge for us;
  if Crouch prices off a model, it is better than anything these feeds give.
* Endgame check (`scripts/endgame_scan.py`, 933 windows, every 0.5 s in the last 40 s, causal Chainlink TWAP):
  when the outcome is practically fixed nobody sells the winner below 0.99. Cheap asks on a "certain" side come with
  |projected TWAP - strike| < $3 or missing Chainlink ticks (all 6 losses). With margin >= $3: 14 windows in 5 days,
  all won, but the ask was gone 1 s later in 12 of 14 (other endgame bots take it): ~$9 / 5 days at $100 per trade.
  Competed away; no.
* 2026-11-02 01:23Z: user deposited ~$25 and asked to run the fast player live: fast-london restarted at $2 per FAK
  (live_scale 1.0, cap $2), live loss limit $15 (room $14 after the earlier -$1), USDC $25.60. fast_london_status now
  flags loss room <= $2.
* Sweep predictor (`scripts/sweep_predict.py`): P(best-bid level gone within 50..1000 ms) from the book (size at the
  best, queue share, level age / shrink, prints, msg rate, futures move); gradient boosting trained 10-24/26/27,
  AUC 0.81 on 10-25, 0.79 on London 11-01; swept levels -1.65 c at 1 s, kept +1.3 c. As a quote guard in the replay
  (`--set sweep`, -swNN = pull a side while P >= 0.NN), 10-25 unseen day, pqb + measured latency: base mk1 -1.18 /
  mk10 -1.61; sw40 -0.08 / -1.04; sw50 -0.14 / -0.91; **always-join + sw40 +0.43 / +0.10** (always-join alone -0.72 /
  -2.72). tt (pessimistic) sw40 -1.69 vs -1.96. First positive 1 s and 10 s markouts on the validated model; 11-01
  London check running.
* 11-01 London (native recordings, unseen by the model), pqb + measured latency: base mk1 -2.09 / mk10 -2.33; always-join
  -1.39 / -1.61; **always-join + sw40 -0.62 / -0.79**; + sw30 -0.94 / -0.86; + sw50 -0.80 / -0.46; tt + sw40 -1.43 / -2.28.
  The guard improves fills by ~0.8-1.5 c on both test days, but 11-01 stays negative (10-25 was +0.43): not a go yet.
* Value guard (2026-11-02): a regressor of the best bid's 10 s value (token mid 10 s later - bid, cents) on the same
  features, trained on all four Dublin days; -vNN = quote a side only while the predicted value >= NN / 10 c. On
  sampled book states it holds out of sample (London 11-02 00-06: quote 52% of the time at v > 0.5 c, realized 10 s
  value +1.37 c vs +0.54 c for always quoting). Replay 11-01 London 08-20 (pqb, measured latency), net c per share:
  **always-join + v0 +0.70 (+$14.17 over 474 fills, $6.75 of it rebates, 86% paired)**; v5 -1.08; v10 -1.98; sw40 with
  the retrained classifier -2.62; tt + v5 -4.22. mk10 still negative (v0 -1.17); 12 h is within noise (sw40 moved
  ~$30 between runs). Check on unseen 11-01 20:00 -> 11-02 07:00 with the unguarded baseline next to it.
* 2026-11-02 07:29Z, user request: value guard live. imb_maker got `value_guard` / `value_cut` (BidValueModel: the
  regressor exported to JSON trees, `backend/bots/bid_value_model.json`, plain Python 0.07 ms per prediction on the
  server, identical to sklearn; the replay's -vNN variants now run this same code). imb-london-live set to variant
  value0 (= replay 10j0-v0: B 5, 1 level, always join, bps guard 0.5 / 1 s, 0.30-0.70, inv / spend cap 6) after a
  6 min dry run (565 simulated orders, 0 rejects); live loss limit 7.10 = $5 fresh room on top of its 11-01 loss of
  -2.10 (5 Down at 0.42). Wallet shared with fast-london (live, $2 FAKs).
* Research option 3, following a trader (`scripts/follow_scan.py`, 10-25..27, every BTC 5m BUY of the 4 studied
  traders): a follower who sees the fill D s later and buys the same token at the mid + 0.5 c + taker fee, held to
  settlement, loses at every delay and for every trader: at 1 s -0.6 (Crouch takers) to -3.5 c/sh (wzbbb makers);
  machinelearned -1.7 (maker fills) / -3.3 (taker fills); 30 s exits no better. Their own edge (-0.65..+2 c) is in
  the entry price itself and is priced in within a second. Ruled out.
* Value guard, 23 h London replay on the tree code (= the live code), pqb + measured latency, net c/sh:
  11-01 08-20 always-join -1.71 / **v0 +1.04** / v5 -1.11 / v0 tt -1.18; unseen 11-01 20 -> 11-02 07 -4.51 / v0 -1.35 /
  v5 -1.19 / v0 tt -1.88; all 23 h -2.95 ($-90) / **v0 -0.09 ($-3.5)**. The guard is worth ~3 c/sh (~$87 / 23 h at
  this size) and brings the maker to breakeven, not yet to a profit. Next: the regressor predicts a bid's value at
  any moment, but only filled moments matter (mostly sweeps) -> retrain on "value given a hit within 1 s".
* **Value-guard live test, 2026-11-02 07:29-08:30Z (imb-london-live, value0): -$6.00 over 11 windows / 127 shares
  (+$0.44 rebates est.), -4.7 c/sh; stopped by the loss limit (lifetime -8.10 vs 7.10; the last open window's -3.30
  overshot the room by $1). Service stopped.** Per window live vs the in-process paper twin (same windows): live
  +1.72 / +1.95 / -1.70 / +0.20 / 0 / +1.70 / -4.02 / -2.27 / -0.27 / -3.30, paper total -2.08 on ~65 shares. Live
  fills ~2x the twin's volume (both sides 10 / 10 where paper had 5), often on the other side (08:00: paper Up 5 @
  0.38 won +3.10, live Down 6 @ 0.67 lost -4.02). Replay expectation at this size ~ -1.7 (-1.35 c/sh x 127); the
  difference is under one window-noise sigma (~$6 over 10 windows), so not conclusive, but no sign of an edge.
* Given-fill targets (sweep_predict extract now also labels `hit` = shares printed into the level 50..1000 ms on,
  from the recording's info lines when no data-api file exists): value of a fill that gets through the queue ahead
  is -2.2 c (11-01) / -2.5 c (11-02 00-07). The all-samples regressor barely separates those (cut 0: -1.8 / -1.5);
  a regressor trained on through-queue samples (bid_value_thru.json) lifts kept fills to -0.9 / -0.6 at cut -0.5
  (keeping 10-13 %); positive only at cuts that keep <= 5 % of fills. Replay of hi / th models on 11-02 00-07 next.
* Given-fill guard replays (pqb + measured latency; net c/sh, fills): unseen 11-02 00-07 with the model trained on
  Dublin + 11-01 (bid_value_thru.json): always-join -6.93 (161), v0 -1.42 (261), **through-model cut -0.5 +2.91 (149),
  cut 0 +2.24 (101), cut -0.5 tt +0.77 (116)**; given-hit model -2.7..-5.5. Unseen 11-01 08-24 with the model trained
  on Dublin only (bid_value_thru_d4.json): always-join -1.76, v0 +0.46, cut -1.0 +1.48, cut -0.5 -1.61, cut 0 +2.90,
  cut -0.5 tt -1.03. mk1 rises cleanly with the cut (-1.29 -> -0.17 / +0.78 / +2.32) but mk10 only reaches ~0, and net
  swings with the settlement of unpaired inventory (+-$40 per 16 h).
* Replay vs the live test, the same 10 windows (07:30-08:15) and config: **live -6.00 on 127 sh (-4.7 c/sh)**;
  replay pqb -3.63 on 136 sh (-2.66), tt -3.38 on 119 (-2.84), queue +1.33 on 176, pq +6.03 on 171. pqb / tt get the
  live volume and sign right and are ~2 c/sh optimistic (as on 10-27); queue / pq are not usable. The in-process paper
  twin (-2.08 on ~65 sh) is not a fill model to trust either.
* **Verdict on the value / sweep guard family (11-02):** it is the best maker filter we have (always-join -2.95 c/sh ->
  ~0 in pqb over 23 h; through-model cuts up to ~+2.7 c/sh pooled on 25 unseen hours at cut 0) but after the measured
  ~2 c/sh pqb -> live gap it is breakeven at best, and the one live test lost $6. No further live runs on it. What
  would change the answer: queue priority (resting at a level before it fills up), which none of these filters give.
* Queue priority test, sticky quotes (imb_maker `sticky_ticks`, paper / replay only, n_levels 1: a resting bid up to
  N ticks below the wanted price stays put and keeps its queue place instead of re-joining at the back), 25 unseen
  London hours 11-01 08:00 -> 11-02 09:00, measured latency, net c/sh (shares; mk1):

  | config | pqb | tt |
  |---|---|---|
  | always join (10j0) | -3.41 (3202; -1.51) | -2.58 (3300; -2.18) |
  | + sticky 1 | -1.65 (3792; -1.59) | -2.08 (3718; -2.34) |
  | + sticky 2 | -1.88 (3876; -1.51) | -1.26 (3891; -2.29) |
  | through guard td0 | **+2.17** (1810; +1.24) | **+2.15** (1497; +1.28) |
  | td0 + sticky 1 | +0.95 (1662; +0.38) | -0.18 (1287; -0.32) |
  | td0 + sticky 2 | +1.57 (1657; +0.80) | +1.11 (1414; -0.11) |
  | base 10 (joins at share >= 0.4) | -2.27 | -3.03 |
  | base 10 + sticky 1 / 2 | -2.91 / -2.69 | -1.35 / -1.48 |

  Sticky adds ~18 % volume at the same mk1 (the per-fill adverse selection does not move); the c/sh changes come from
  the unpaired settlement (+-$40) and flip sign between configs and fill models. With the guard it costs 0.6-2.3 c/sh
  in both models. **Ruled out.** The through guard td0 (Dublin-trained, so all 25 h are unseen) holds at +2.2 c/sh in
  both pqb and tt, i.e. ~+0.2 after the ~2 c/sh replay -> live gap: breakeven, as in the verdict above. Logs:
  data/replays/sticky_25h.log, sticky_base_tt.log.
* **Maker status 2026-11-02, final for this round:** no imb_maker variant is a go. Best = through-queue value guard
  at ~breakeven after calibration; live lifetime -8.10. imb-london-live stopped, nothing resting. A real edge would
  need a true queue-front position (resting before the level fills, i.e. quoting price levels ahead of the market),
  which these recordings cannot replay credibly (the queue / pq models are the ones that fail calibration).

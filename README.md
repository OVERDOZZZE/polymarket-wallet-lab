# Wallet Lab

Trade, run bots and reverse-engineer wallets on Polymarket's crypto **Up or Down** windows (BTC 5m/15m).

**Start:** double-click `run_app.bat` and the app opens at http://127.0.0.1:8000.
Closing the window stops the server. Bots run in their own processes and keep going.

For how the code is organised, see [STRUCTURE.md](STRUCTURE.md).

## Trade

Manual live trading with the wallet in `.env.live`, laid out like Polymarket.

- **Chart:** Chainlink (what the window settles on) with Coinbase, Kraken and Binance overlaid. Click a
  feed chip to hide it. *Align to Chainlink* shifts the exchanges by their median gap, so you can see who
  leads. Each chip shows that feed's latency to your browser.
- **Price to beat** is the Chainlink tick at the window's first second, with the countdown beside it.
  Switch between 5 min / 15 min and the current / next window at the top right.
- **Order ticket:** Buy/Sell, Market/Limit, Up/Down.
  - Market orders are fill-and-kill with a price limit: the book's worst level plus your slippage.
  - Clicking a price in the order book fills it in as a limit price.
- **Safety:**
  - Orders are refused until you switch on **Live trading** in the ticket. It switches itself off whenever
    the server restarts.
  - Any single order above `LIVE_MAX_ORDER_USD` (default $500) is refused.
- Positions, open orders (with cancel and cancel all), fills and a full order log sit below the chart.
  Every order and fill is saved.
- **Latency:** prices stream from each exchange straight into the browser. Orders go over an already-open
  connection to the server, which signs them in about 0.7 ms and posts on a pre-warmed connection.
  The Latency card shows every hop.

## Bots

Each bot has its own process, its own paper bankroll and its own history in the database.

- **New bot:** pick a strategy and a bankroll.
- **Bot page:** portfolio, PnL chart, positions, open orders, activity, live markets, history and
  diagnostics.
- **New account:** starts a fresh paper bankroll. The old results stay saved.

## Traders

Track any wallet: paste a 0x address, an @username or a profile link.

- Their history downloads in the background.
- The analysis covers PnL, timing, entry prices, signal tests and a strategy scorecard.
- **Live:** streams their trades about 0.3 s after they happen.
- The Traders list compares everyone side by side.

## Files

| Path | What |
|---|---|
| `.env` / `.env.live` | Settings and secrets (templates: `.env.example`, `.env.live.example`) |
| `data/btc5m.db` | The database: traders, their live events, bots and all their actions, your live orders and fills |
| `data/traders/` | Downloaded wallet activity and analysis outputs |
| `data/bots/` | Running bots' live state and logs |
| `data/cache/` | Shared market resolutions and 1 s reference prices |
| `archive/` | The previous (v1) app and its raw files |

## Spike-taker paper bot (`spikebot/`)

A small standalone paper bot, separate from the app above. It doesn't import from `backend/` or `research/`,
and it never places real orders. Its strategy is the profitable part of what @hcrystallash and @wzbbb do on
BTC 5-minute Up/Down. When Binance BTCUSDT moves 1.5 bps or more within 1 s, it buys the token that move
favours, as long as that token's ask hasn't repriced yet. Positions are held to settlement. Every parameter is
in `spikebot/config.py`.

The paper account starts with $1,000. Each simulated buy is matched against the live Polymarket book 500 ms
after the signal, which stands in for Polymarket's taker delay. It takes the asks up to the signal price + 2 c
and pays the taker fee of 0.07 × p × (1 − p) per share. Winners pay $1 per share once Gamma shows the result.

| Task | Command (Git Bash, from the project root) |
|---|---|
| Start, auto-restart on crash | `nohup .venv/Scripts/python -m spikebot.supervise > logs/spikebot_supervisor.out 2>&1 &` |
| Stop | `.venv/Scripts/python -m spikebot.stop` (the bot and its supervisor exit within ~5 s) |
| Results | `.venv/Scripts/python -m spikebot.report` |
| Run in the foreground (testing) | `.venv/Scripts/python -m spikebot` |

From PowerShell, use `.venv\Scripts\python` instead and run the start command without `nohup` and `&`, or start it
with `Start-Process -WindowStyle Hidden`.

Output goes to `logs/` (git-ignored):

| File | What |
|---|---|
| `spikebot.db` | SQLite: `signals` (every signal acted on or skipped, with the reason), `trades` (fills, the book levels taken), `windows` (positions, winner, PnL), `equity` (once a minute), `events` (start, stop, config) |
| `spikebot.log` | text log: FILL / MISS / SETTLE lines, warnings |
| `spikebot_status.json` | live snapshot every 5 s: cash, feed ages, counters |
| `spikebot_supervisor.log` | restarts |

A restart continues the same account, because cash is rebuilt from the database. Delete `logs/` to start over at
$1,000. The only requirements are `websockets` and `requests` (`spikebot/requirements.txt`), and the project's
`.venv` already has both.

### Live mode (real money, Dublin server)

The `live` variant runs main's strategy and also sends a real fill-and-kill buy of $3 for each signal. The worst
price is the signal ask + 2 c, and the order goes out the moment the signal fires. It uses the app's trading
Gateway (`backend/trading/gateway.py`, the only code that reads `.env.live`), so it runs from the app folder on the
server (`~/app`). A hard budget refuses any buy once realized live loss + cost still open + the new order would
pass $10 (`LIVE_BUDGET_USD`). `live-dry` runs the same flow and sends nothing.

| Task | Command (on the server, in `~/app`) |
|---|---|
| Start live | `nohup .venv/bin/python -m spikebot.supervise --variant live > logs/live/supervisor.out 2>&1 &` |
| Stop live | `.venv/bin/python -m spikebot.stop --variant live` |
| Live results | `.venv/bin/python -m spikebot.report --variant live` (the "LIVE" lines; orders also appear in the app's `live_orders` table with origin `spikebot-live`) |

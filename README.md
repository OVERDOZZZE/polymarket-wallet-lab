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

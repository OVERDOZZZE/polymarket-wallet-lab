"""Spike-taker paper bot: every parameter, hard-coded (from the 2026-10-27 study of @hcrystallash / @wzbbb).

The strategy is the profitable core of what both traders do on Polymarket's BTC 5-minute Up/Down windows: when
Binance BTCUSDT jumps, buy the token that the jump favours before Polymarket's makers reprice it, and hold it to
settlement. Paper only: this package has no order-signing code and never talks to the trading API."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
DB_PATH = LOG_DIR / "spikebot.db"
LOG_PATH = LOG_DIR / "spikebot.log"
STATUS_PATH = LOG_DIR / "spikebot_status.json"
STOP_PATH = LOG_DIR / "spikebot.STOP"
PID_PATH = LOG_DIR / "spikebot.pid"
SUPERVISOR_PID_PATH = LOG_DIR / "spikebot_supervisor.pid"
SUPERVISOR_LOG_PATH = LOG_DIR / "spikebot_supervisor.log"

# endpoints (public market data only)
GAMMA = "https://gamma-api.polymarket.com"
CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"   # spot best bid/ask; mid = signal price

# bankroll
BANKROLL_USD = 1000.0

# signal: Binance spot mid moves >= THETA_BPS within the trailing SIGNAL_WINDOW_MS
SIGNAL_WINDOW_MS = 1000
THETA_BPS = 1.5

# which windows / prices to trade
WINDOW_S = 300                 # BTC 5-minute windows only
T_MIN_S = 5                    # no entries in a window's first 5 s
T_END_S = 15                   # ... nor in its last 15 s
P_MIN, P_MAX = 0.15, 0.60      # favoured token's best ask must be inside this range (the edge is in the cheaper
                               # token: backtest +12..19 c/share at 0.15-0.60 vs +1..4 c at 0.60-0.85)
MAX_REPRICE = 0.02             # skip when that ask already rose > 2 c since the move began (quote no longer stale)
COOLDOWN_S = 5                 # at most one entry per 5 s per window

# execution model: a fill-and-kill buy, matched against the live book SIM_LATENCY_MS after the signal
SIM_LATENCY_MS = 500           # Polymarket holds taker orders ~0.2-2.7 s (median ~0.46 s measured from Dublin)
SLIPPAGE = 0.02                # limit price = signal ask + 2 c; book levels above it are not taken
FEE_RATE = 0.07                # taker fee per share = FEE_RATE * p * (1 - p), paid in USDC

# sizing: a fixed $ amount per order (the traders' constant-fee rule, shares ~ 1 / (p (1 - p)), put the most money
# on the expensive tokens, where the backtest edge is smallest)
ORDER_USD = 20.0
MIN_SHARES = 5.0               # Polymarket's minimum order size
MAX_WINDOW_USD = 120.0         # total spent in one window

# Named variants (run with --variant NAME; each has its own logs/<name>/ folder and $1,000 account).
# "main" = the strategy above. "wide" = a looser comparison run (more trades, smaller backtest edge per share).
VARIANTS = {
    "main": {},
    "wide": {"THETA_BPS": 1.0, "P_MAX": 0.85},
}


def apply_variant(name):
    """Switch this module's settings to a variant; non-main variants log under logs/<name>/."""
    g = globals()
    g.update(VARIANTS[name])
    g["VARIANT"] = name
    if name != "main":
        d = LOG_DIR / name
        g.update(LOG_DIR=d, DB_PATH=d / "spikebot.db", LOG_PATH=d / "spikebot.log",
                 STATUS_PATH=d / "spikebot_status.json", STOP_PATH=d / "spikebot.STOP", PID_PATH=d / "spikebot.pid",
                 SUPERVISOR_PID_PATH=d / "spikebot_supervisor.pid", SUPERVISOR_LOG_PATH=d / "spikebot_supervisor.log")


VARIANT = "main"

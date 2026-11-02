"""Paths, settings and secrets. Every other module imports locations from here - never hard-code a path.

.env       public-ish settings + CLOB L2 API creds (POLY_API_*), DB_PATH
.env.live  the trading wallet (POLYMARKET_*). Read only by backend.trading; never logged, never sent to the UI.
"""
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}


def _path(value, default):
    p = Path(value or default)
    return p if p.is_absolute() else ROOT / p


DATA_DIR = ROOT / "data"
DB_PATH = _path(ENV.get("DB_PATH"), "data/btc5m.db")
TRADERS_DIR = DATA_DIR / "traders"           # traders/<id>/activity.parquet, analysis/
BOTS_DIR = DATA_DIR / "bots"                 # bots/<id>/state.json, control.json, bot.log, bot.pid
CACHE_DIR = DATA_DIR / "cache"               # resolutions.json, klines/<SYMBOL>/<day>.csv
RECORDINGS_DIR = DATA_DIR / "recordings"       # recordings/<day>/<HH>.tsv.gz raw market feeds (backend/recorder.py)
IMPORT_DIR = DATA_DIR / "import"               # data from other machines; btc5m/ = the old logger PC (scripts/import_btc5m.py)
LATENCY_DIR = DATA_DIR / "latency"             # raw latency measurements (scripts/latency_probe.py, latency_history.py)
LATENCY_PROFILE = ROOT / "backend" / "bots" / "latency_profile.json"   # measured delays for simulations (bots/latency.py)
BID_VALUE_MODEL = ROOT / "backend" / "bots" / "bid_value_model.json"   # imb_maker value guard (scripts/sweep_predict export)
FRONTEND_DIST = ROOT / "frontend" / "dist"

HOST = ENV.get("HOST", "127.0.0.1")
PORT = int(ENV.get("PORT", "8000"))

# Polymarket endpoints (shared by every module)
CLOB_HOST = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
RTDS_WS = "wss://ws-live-data.polymarket.com"
CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_USER_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
PM_CRYPTO_PRICE = "https://polymarket.com/api/crypto/crypto-price"   # SPOT tick at a boundary: NOT what settles (see markets.py)
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
BINANCE_FUT_WS = "wss://fstream.binance.com/market/ws/btcusdt@aggTrade"   # USD-M futures trades (~40 msg/s): BTC timing signal only. Not /ws/ (silent since Binance moved it) nor bookTicker (~900 msg/s)
KRAKEN_WS = "wss://ws.kraken.com/v2"
CHAIN_ID = 137


def live_env():
    """Trading secrets from .env.live (fresh read each call, so edits apply on restart of the gateway)."""
    vals = {k: v for k, v in dotenv_values(ROOT / ".env.live").items() if v}
    vals.update({k: v for k, v in ENV.items() if k.startswith("POLY_API_")})
    return vals


for d in (DATA_DIR, TRADERS_DIR, BOTS_DIR, CACHE_DIR, RECORDINGS_DIR):
    d.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTHONUTF8", "1")

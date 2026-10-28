"""Up/Down window markets: assets, window math, slugs, and market lookup on Gamma.

A window market's slug is  <asset>-updown-<5|15>m-<window start epoch>, e.g. btc-updown-5m-1792594200.
Settlement (verified 2026-10-21 against Polymarket's eventMetadata): the market resolves on Chainlink's
"btc-usd-twap-60s" stream - the 60 s TWAP of btc/usd at the window's start (price to beat) and end (final
price); Up wins if final >= price to beat. That stream is not on RTDS, so it is computed from the 1 s
btc/usd ticks (twap_of). The single tick on the boundary second, and polymarket.com/api/crypto/crypto-price
(which returns that tick), disagree with the outcome in ~12% of windows.
"""
import json
import re
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from ..config import GAMMA

NY = ZoneInfo("America/New_York")

# asset code -> slug prefix, title word, Binance symbol (historical reference price), Chainlink symbol
ASSETS = {
    "BTC": {"slug": "btc", "title": "bitcoin", "symbol": "BTCUSDT", "chainlink": "btc/usd"},
    "ETH": {"slug": "eth", "title": "ethereum", "symbol": "ETHUSDT", "chainlink": "eth/usd"},
    "SOL": {"slug": "sol", "title": "solana", "symbol": "SOLUSDT", "chainlink": "sol/usd"},
    "XRP": {"slug": "xrp", "title": "xrp", "symbol": "XRPUSDT", "chainlink": "xrp/usd"},
}
WINDOW_CHOICES = [5, 15]
VARIANT = {5: "fiveminute", 15: "fifteen"}          # name used by polymarket.com/api/crypto/crypto-price
TWAP_S = 60                                          # Chainlink btc-usd-twap-60s
TWAP_END_LAG = 3                                     # ... over the ticks observed up to 3 s before the boundary
TWAP_MIN_TICKS = 50                                  # need most of the minute's 1 s ticks to trust our TWAP


def twap_secs(sec):
    """The seconds whose Chainlink ticks make up the 60 s TWAP at boundary `sec`: [sec - 62, sec - 3]. Fitted
    2026-10-27 on ~950 windows against priceToBeat: exact to the cent in about half, median error 2 c (the former
    (sec - 60, sec]: 75 c). So a window's outcome is fixed 3 s before it ends."""
    hi = int(sec) - TWAP_END_LAG
    return range(hi - TWAP_S + 1, hi + 1)


def twap_of(ticks, sec):
    """60 s TWAP at boundary second `sec` from {second: Chainlink value} (1 tick per second, stamped on the
    second): the mean of the ticks in twap_secs(sec). None if fewer than TWAP_MIN_TICKS of them are present."""
    xs = [ticks[s] for s in twap_secs(sec) if s in ticks]
    return sum(xs) / len(xs) if len(xs) >= TWAP_MIN_TICKS else None


def fetch_window_prices(slug):
    """Polymarket's own price to beat / final price of a window (event metadata; the TWAPs it settles on).
    priceToBeat appears once the previous window has resolved, finalPrice once this one has. {} if neither."""
    js = HTTP.get(f"{GAMMA}/events", params={"slug": slug}, timeout=10).json()
    meta = (js[0].get("eventMetadata") if js else None) or {}
    return {k: float(meta[k]) for k in ("priceToBeat", "finalPrice") if meta.get(k) is not None}

SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)m-(\d{10})$")
TITLE_RE = re.compile(
    r"(\w+) (\d{1,2}), (\d{1,2}:\d{2})\s?(AM|PM)\s?-\s?(\d{1,2}:\d{2})\s?(AM|PM)", re.I)

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Mozilla/5.0 (wallet-lab)"


def window_bounds(window_minutes, t=None):
    L = window_minutes * 60
    t = time.time() if t is None else t
    start = int(t) // L * L
    return start, start + L


def window_slug(asset, window_minutes, start):
    return f"{ASSETS[asset]['slug']}-updown-{window_minutes}m-{int(start)}"


def slug_start(slug):
    m = SLUG_RE.match(str(slug or ""))
    return int(m.group(3)) if m else None


def fetch_market(slug):
    """Gamma lookup of one window market -> dict with tokens and trading parameters, or None."""
    js = HTTP.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10).json()
    if not js:
        return None
    m = js[0]
    outs = [str(o).lower() for o in json.loads(m.get("outcomes") or "[]")]
    toks = json.loads(m.get("clobTokenIds") or "[]")
    cmc = m.get("cryptoMarketConfig") or {}
    fs = m.get("feeSchedule") or {}
    mm = SLUG_RE.match(slug)
    return {"slug": slug, "tokens": dict(zip(outs, toks)), "conditionId": m.get("conditionId"),
            "title": m.get("question") or m.get("title"), "end": m.get("endDate"),
            "start": int(mm.group(3)) if mm else None, "minutes": int(mm.group(2)) if mm else None,
            "tick": float(m.get("orderPriceMinTickSize") or 0.01), "min_size": float(m.get("orderMinSize") or 5),
            "neg_risk": bool(m.get("negRisk")), "twap": cmc.get("twapEnabled", True),
            "twap_s": cmc.get("twapLookbackSeconds") or 60, "fee_rate": fs.get("rate"),
            "rebate_rate": fs.get("rebateRate"), "accepting": bool(m.get("acceptingOrders", True))}


def fetch_winner(slug):
    """'up' / 'down' once Gamma shows the market resolved, else None."""
    js = HTTP.get(f"{GAMMA}/markets", params={"slug": slug, "closed": "true"}, timeout=10).json()
    if not js:
        return None
    m = js[0]
    try:
        outs = [str(o).lower() for o in json.loads(m.get("outcomes") or "[]")]
        px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except (TypeError, ValueError):
        return None
    for o, p in zip(outs, px):
        if p >= 0.99:
            return o
    return None


# ---------------------------------------------------------------- historical activity rows -> windows
def parse_market(slug, title, ts, asset="BTC"):
    """(window_start_epoch, window_len_seconds) if the market is one of `asset`'s Up/Down windows, else None."""
    a = ASSETS[asset]
    slug = str(slug or "").lower()
    title = str(title or "")
    m = SLUG_RE.match(slug)
    if m:
        if m.group(1) != a["slug"]:
            return None
        return int(m.group(3)), int(m.group(2)) * 60
    if not title.lower().startswith(f"{a['title']} up or down"):
        return None
    m = TITLE_RE.search(title)
    if not m:
        return None
    mon, day, t1, ap1, t2, ap2 = m.groups()
    try:
        year = pd.Timestamp(int(ts), unit="s", tz="UTC").tz_convert(NY).year
        fmt = "%B %d %Y %I:%M%p"
        s = pd.to_datetime(f"{mon} {day} {year} {t1}{ap1.upper()}", format=fmt).tz_localize(NY)
        e = pd.to_datetime(f"{mon} {day} {year} {t2}{ap2.upper()}", format=fmt).tz_localize(NY)
    except Exception:
        return None
    if e <= s:
        e += pd.Timedelta(days=1)
    return int(s.timestamp()), int((e - s).total_seconds())


def market_map(df, asset, window_minutes):
    """slug -> (window_start, window_len) for every matching market in an activity frame."""
    out = {}
    if df is None or df.empty or "slug" not in df:
        return out
    for slug, title, ts in df[["slug", "title", "timestamp"]].drop_duplicates("slug").itertuples(index=False):
        p = parse_market(slug, title, ts, asset)
        if p and p[1] == window_minutes * 60:
            out[slug] = p
    return out

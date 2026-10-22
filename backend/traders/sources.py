"""Remote data for trader analysis: Polymarket activity / profiles / resolutions and Binance 1s klines."""
import re
import time

import requests

from ..config import CLOB_HOST as CLOB, DATA_API, GAMMA

BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
ACTIVITY_PAGE = 500
ACTIVITY_MAX_OFFSET = 5000  # the data-api refuses deeper offsets

SESSION = requests.Session()
SESSION.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

WALLET_RE = re.compile(r"0x[a-fA-F0-9]{40}")
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_.\-]{2,64})$")


class FetchError(Exception):
    pass


class Cancelled(Exception):
    """Raised when a job is cancelled. `partial` / `reached_ts` carry activity rows already
    downloaded, which are complete for (reached_ts, end]."""

    def __init__(self, partial=None, reached_ts=None):
        super().__init__("cancelled")
        self.partial, self.reached_ts = partial, reached_ts


def get_json(url, params=None, retries=6, cancel=None):
    """GET with retries on 429/5xx/network errors. 4xx errors fail immediately."""
    last = None
    for i in range(retries):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            r = SESSION.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            last = f"network error: {e}"
            time.sleep(min(1 + i * 2, 15))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(min(2 ** i, 30))
            continue
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            raise FetchError(f"non-JSON response from {url}: {r.text[:200]}")
    raise FetchError(f"{url} failed after {retries} attempts ({last})")


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------
def parse_identity(text):
    """Accepts a 0x address, @handle, handle, or a polymarket.com profile URL.
    Returns ("wallet", address) or ("handle", name). Raises ValueError if unusable."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Enter a wallet address, @handle or profile URL.")
    m = WALLET_RE.search(t)
    if m:
        return "wallet", m.group(0).lower()
    if "polymarket.com" in t:
        m = re.search(r"polymarket\.com/@([^/?#\s]+)", t)
        if m:
            return "handle", m.group(1)
        raise ValueError("Could not find an address or @handle in that URL.")
    m = HANDLE_RE.match(t)
    if m:
        return "handle", m.group(1)
    raise ValueError("That doesn't look like a wallet address (0x + 40 hex chars) or a handle.")


def resolve_handle(handle, cancel=None):
    """Return (proxyWallet, profile dict) for a Polymarket username."""
    js = get_json(f"{GAMMA}/public-search", {"q": handle, "search_profiles": "true"}, cancel=cancel)
    profiles = (js or {}).get("profiles") or []
    h = handle.lower()
    for p in profiles:
        names = [str(p.get("name") or "").lower(), str(p.get("pseudonym") or "").lower()]
        if h in names and p.get("proxyWallet"):
            return p["proxyWallet"].lower(), p
    near = ", ".join(str(p.get("name") or p.get("pseudonym")) for p in profiles[:5])
    raise FetchError(f"No profile named '{handle}' found."
                     + (f" Close matches: {near}." if near else "")
                     + " Paste the 0x address from the profile page instead.")


# --------------------------------------------------------------------------
# activity
# --------------------------------------------------------------------------
def fetch_activity(wallet, start_ts, end_ts, log=print, progress=None, cancel=None):
    """All activity rows with start_ts <= timestamp <= end_ts, paging backwards in time
    with the inclusive `end` cursor (offset paging is capped at 5000).

    Pages never overlap, so no de-duplication is needed - which matters because one
    transaction often contains several genuinely identical fills (same size and price
    against different makers) that must all be kept. Rows at a page's oldest second may
    be cut off, so they are dropped and re-read in full by the next page."""
    rows = []           # complete: every row with timestamp > cursor
    same = []           # rows at exactly `cursor`, collected while walking the offset
    cursor, offset = int(end_ts), 0
    span = max(1, end_ts - start_ts)
    while True:
        params = {"user": wallet, "limit": ACTIVITY_PAGE, "offset": offset, "start": int(start_ts),
                  "end": cursor, "sortBy": "TIMESTAMP", "sortDirection": "DESC"}
        try:
            page = get_json(f"{DATA_API}/activity", params, cancel=cancel)
        except Cancelled:
            raise Cancelled(list(rows), cursor) from None  # rows newer than the cursor are complete
        if isinstance(page, dict):
            raise FetchError(f"activity API error: {str(page)[:200]}")
        page = page or []
        if offset:  # walking one crowded second
            same += [r for r in page if int(r["timestamp"]) == cursor]
            done_second = len(page) < ACTIVITY_PAGE or any(int(r["timestamp"]) < cursor for r in page)
            if offset + len(page) >= ACTIVITY_MAX_OFFSET and not done_second:
                log(f"warning: more than {ACTIVITY_MAX_OFFSET} rows in one second ({cursor}); some skipped")
                done_second = True
            if not done_second:
                offset += len(page)
                continue
            rows += same
            same, offset = [], 0
            if cursor <= start_ts:
                break
            cursor -= 1
            continue
        if len(page) < ACTIVITY_PAGE:  # last page: everything down to start_ts is here
            rows += page
            break
        mn = min(int(r["timestamp"]) for r in page)
        if mn == cursor:  # the whole page is one second: switch to offset walking
            same, offset = list(page), len(page)
            continue
        rows += [r for r in page if int(r["timestamp"]) > mn]
        cursor = mn
        if progress:
            progress(min(1.0, (end_ts - mn) / span),
                     f"{len(rows):,} rows, back to {time.strftime('%Y-%m-%d %H:%M', time.gmtime(mn))} UTC")
        time.sleep(0.12)
    return [r for r in rows if start_ts <= int(r["timestamp"]) <= end_ts]


# --------------------------------------------------------------------------
# resolutions
# --------------------------------------------------------------------------
def fetch_resolution(condition_id, cancel=None):
    """Returns {"winner": "up"/"down"/None, "closed": bool}."""
    m = get_json(f"{CLOB}/markets/{condition_id}", cancel=cancel, retries=4)
    toks = m.get("tokens") or []
    winner = None
    for t in toks:
        if t.get("winner") is True:
            winner = str(t.get("outcome")).lower()
    if winner is None and toks and m.get("closed"):
        best = max(toks, key=lambda t: float(t.get("price") or 0))
        if float(best.get("price") or 0) >= 0.99:
            winner = str(best.get("outcome")).lower()
    return {"winner": winner, "closed": bool(m.get("closed"))}


# --------------------------------------------------------------------------
# Binance
# --------------------------------------------------------------------------
def fetch_klines_1s(symbol, t0, t1, cancel=None):
    """1s close prices for [t0, t1) as a list of (epoch_s, close). Tries each host."""
    errors = []
    for host in BINANCE_HOSTS:
        try:
            cur, rows = t0 * 1000, []
            while cur < t1 * 1000:
                js = get_json(f"{host}/api/v3/klines",
                              {"symbol": symbol, "interval": "1s", "startTime": cur,
                               "endTime": t1 * 1000 - 1, "limit": 1000}, cancel=cancel, retries=4)
                if not js:
                    break
                rows += [(int(k[0]) // 1000, float(k[4])) for k in js]
                cur = int(js[-1][0]) + 1000
                time.sleep(0.03)
            if rows:
                return rows
        except Cancelled:
            raise
        except Exception as e:
            errors.append(f"{host}: {e}")
    if errors:
        raise FetchError("Binance klines failed - " + " | ".join(errors))
    return []

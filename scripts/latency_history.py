"""Latency history (server, from ~/app): what the bots really got, from the recorder's files and every real order.

    nice -n 10 .venv/bin/python -m scripts.latency_history [--from "2026-10-24 00:00"] [--workers 3]

One pass over data/recordings/<day>/<HH>.tsv.gz (one worker per hour file, stamps = the recorder's receive time on
this chrony-synced clock), plus the DB's live_orders and the imb_maker bots' per-window logs:

  feeds        age = recorder receive - the venue's timestamp, per source: Binance futures aggTrade (event E, trade T),
               Coinbase ticker/matches time, Kraken ticker timestamp, Chainlink (RTDS publish, observation), every
               Polymarket book message (5m and 15m); gaps between messages per source (bn/bf/cb/kr/cl; book per
               window socket). By hour too (worst hours).
  reaction     after a Binance futures 1 s move >= 1 / 2 bps (BTC 5m, 10-285 s into the window, favoured ask
               0.05-0.95): ms until the favoured token's best ask first rises (1 tick) and rises >= 2 c; the ask
               change after 100/250/500/1000/2000 ms (how long a stale ask lives once we see the move).
  fak          every real FAK (market) order: sign, post round trip; at send the best ask on its side vs its limit;
               when the asks at the limit vanished (first moment best ask > limit) relative to send and to the
               response: the queue model's gap. Filled vs killed.
  maker        imb-maker-live / imb-maker-10 post-only orders (window logs): place ack, cancel ack; real maker
               fills: notice received - the engine's time of the matching public trade print.

Output: data/latency/history-<utc>.json ({n, mean, min, p1..p99.9, max, q} per measure). Timings only.
"""
import argparse
import bisect
import gzip
import json
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

import orjson
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bots import replay  # noqa: E402
from backend.config import BOTS_DIR, DB_PATH, GAMMA, LATENCY_DIR, RECORDINGS_DIR  # noqa: E402

OUT_DIR = LATENCY_DIR
MAKER_BOTS = ("imb-maker-live", "imb-maker-10")
TRIGGERS = (1.0, 2.0)
PROBE_MS = (100, 250, 500, 1000, 2000)
REACT_MAX_MS = 10000

RE_E = re.compile(r'"E":(\d+)')
RE_T = re.compile(r'"T":(\d+)')
RE_P = re.compile(r'"p":"([\d.]+)"')
RE_PM_TS = re.compile(r'"timestamp":"?(\d{13})')
RE_CL_OUT = re.compile(r'"timestamp":(\d{13}),"topic"')
RE_CL_OBS = re.compile(r'"symbol":"btc/usd","timestamp":(\d{13})')


class Hist:
    """Histogram, 0.5 ms bins over -200..20000 ms, exact outside; mergeable across workers."""
    LO, HI, W = -200.0, 20000.0, 0.5
    NB = int((HI - LO) / W)

    def __init__(self):
        self.c, self.out, self.n, self.sum = {}, [], 0, 0.0

    def add(self, x):
        self.n += 1
        self.sum += x
        if self.LO <= x < self.HI:
            i = int((x - self.LO) / self.W)
            self.c[i] = self.c.get(i, 0) + 1
        else:
            self.out.append(x)

    def merge(self, o):
        for i, k in o.c.items():
            self.c[i] = self.c.get(i, 0) + k
        self.out += o.out
        self.n += o.n
        self.sum += o.sum

    def quantile(self, p):
        if not self.n:
            return None
        k = p * (self.n - 1)
        lo = sorted(x for x in self.out if x < self.LO)
        if k < len(lo):
            return lo[int(k)]
        k -= len(lo)
        for i in sorted(self.c):
            if k < self.c[i]:
                return self.LO + (i + 0.5) * self.W
            k -= self.c[i]
        hi = sorted(x for x in self.out if x >= self.HI)
        return hi[min(int(k), len(hi) - 1)] if hi else self.HI

    def summary(self, quantiles=True):
        if not self.n:
            return {"n": 0}
        lo = [x for x in self.out if x < self.LO]
        hi = [x for x in self.out if x >= self.HI]
        mn = min(lo) if lo else self.LO + min(self.c) * self.W
        mx = max(hi) if hi else self.LO + (max(self.c) + 1) * self.W
        s = {"n": self.n, "mean": self.sum / self.n, "min": mn}
        for p in (1, 5, 10, 25, 50, 75, 90, 95, 99, 99.9):
            s[f"p{p:g}"] = self.quantile(p / 100)
        s["max"] = mx
        if quantiles:
            s["q"] = [round(self.quantile(i / 100), 2) for i in range(101)]
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}


def exact(xs, quantiles=True):
    """Exact summary of a list (linear interpolation between order statistics)."""
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}

    def q(p):
        k = (n - 1) * p
        i = int(k)
        j = min(i + 1, n - 1)
        return xs[i] + (xs[j] - xs[i]) * (k - i)
    s = {"n": n, "mean": sum(xs) / n, "min": xs[0]}
    for p in (1, 5, 10, 25, 50, 75, 90, 95, 99, 99.9):
        s[f"p{p:g}"] = q(p / 100)
    s["max"] = xs[-1]
    if quantiles:
        s["q"] = [round(q(i / 100), 2) for i in range(101)]
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}


def _iso_ms(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


# ------------------------------------------------------------------ one hour file
def hour_pass(args):
    path, toks, faks, watch_slugs = args
    H = defaultdict(Hist)                                  # label -> Hist
    last = {}                                              # gap key -> last recv ms
    best_ask = {}                                          # token -> best ask (5m windows)
    asks = {}                                              # token -> {price: size} for book snapshots
    bf = deque()                                           # (recv ms, price)
    cool = {th: 0.0 for th in TRIGGERS}
    pend = []                                              # reaction probes in flight
    react = []                                             # finished reaction probes
    fak_open = sorted(faks, key=lambda o: o["t_send"])
    fi = 0
    fak_live = []
    fak_done = []
    prints = []                                            # (recv, engine ts, token, price, size) for watched slugs
    hour = path.parent.name + " " + path.name[:2]

    def gap(key, r, label):
        p = last.get(key)
        if p is not None:
            H[label].add(r - p)
        last[key] = r

    for line in replay._gz_lines(path):
        try:
            ms, src, raw = line.split("\t", 2)
            r = float(ms)
        except ValueError:
            continue
        while fi < len(fak_open) and fak_open[fi]["t_send"] <= r:           # FAKs sent: snapshot their side
            o = fak_open[fi]
            fi += 1
            o["ask_at_send"] = best_ask.get(o["token"])
            o["vanish"] = None if (o["ask_at_send"] is not None and o["ask_at_send"] <= o["limit"] + 1e-9) else 0.0
            fak_live.append(o)
        if src == "bf":
            mE, mT, mP = RE_E.search(raw), RE_T.search(raw), RE_P.search(raw)
            if mE:
                H["bf.age_event"].add(r - int(mE.group(1)))
            if mT:
                H["bf.age_trade"].add(r - int(mT.group(1)))
                if mE:
                    H["bf.venue_E_minus_T"].add(int(mE.group(1)) - int(mT.group(1)))
            gap("bf", r, "bf.gap")
            if not mP:
                continue
            px = float(mP.group(1))
            bf.append((r, px))
            while len(bf) > 1 and bf[1][0] <= r - 1000:
                bf.popleft()
            ref = bf[0][1] if bf[0][0] <= r - 1000 else None
            if ref is None:
                continue
            mv = (px / ref - 1) * 1e4
            start = int(r / 1000) // 300 * 300
            tin = r / 1000 - start
            slug = f"btc-updown-5m-{start}"
            for th in TRIGGERS:
                if abs(mv) < th or r - cool[th] < 2000 or not (10 <= tin <= 285) or slug not in toks:
                    continue
                cool[th] = r
                side = "up" if mv > 0 else "down"
                tok = toks[slug][side]
                a0 = best_ask.get(tok)
                if a0 is None or not (0.05 <= a0 <= 0.95):
                    continue
                pend.append({"th": th, "t0": r, "slug": slug, "tok": tok, "a0": a0, "up1": None, "up2": None,
                             "at": {}})
        elif src == "bn":
            gap("bn", r, "bn.gap")
        elif src == "cb":
            i = raw.find('"time":"')
            if i >= 0:
                try:
                    H["cb.age_event"].add(r - _iso_ms(raw[i + 8:raw.index('"', i + 8)]))
                except ValueError:
                    pass
            gap("cb", r, "cb.gap")
        elif src == "kr":
            i = raw.find('"timestamp":"')
            if i >= 0:
                try:
                    H["kr.age_event"].add(r - _iso_ms(raw[i + 13:raw.index('"', i + 13)]))
                except ValueError:
                    pass
            gap("kr", r, "kr.gap")
        elif src == "cl":
            if '"btc/usd"' not in raw or '"update"' not in raw:
                continue
            m1, m2 = RE_CL_OUT.search(raw), RE_CL_OBS.search(raw)
            if m1:
                H["cl.age_publish"].add(r - int(m1.group(1)))
            if m2:
                H["cl.age_observation"].add(r - int(m2.group(1)))
            gap("cl", r, "cl.gap")
        elif src.startswith("bk:"):
            slug = src[3:]
            five = slug.startswith("btc-updown-5m-")
            fam = "pm5" if five else "pm15"
            gap(src, r, f"{fam}.gap")
            if not five:
                m = RE_PM_TS.search(raw)
                if m:
                    H["pm15.age"].add(r - int(m.group(1)))
                continue
            # probes read the state BEFORE this message (the book as it stood until r)
            for p in pend:
                if p["slug"] == slug:
                    for d in PROBE_MS:
                        if d not in p["at"] and r >= p["t0"] + d:
                            p["at"][d] = best_ask.get(p["tok"])
            try:
                d = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            first = True
            for e in d if isinstance(d, list) else [d]:
                if not isinstance(e, dict):
                    continue
                ts = e.get("timestamp")
                et = e.get("event_type")
                if ts and first:
                    H["pm5.age"].add(r - float(ts))
                    if et:
                        H[f"pm5.age.{et}"].add(r - float(ts))
                    first = False
                if et == "book":
                    t = e.get("asset_id")
                    a = {float(x["price"]): float(x["size"]) for x in e.get("asks", [])}
                    asks[t] = a
                    best_ask[t] = min(a) if a else None
                elif et == "price_change":
                    for c in e.get("price_changes", []):
                        t = c.get("asset_id")
                        ba = c.get("best_ask")
                        if ba not in (None, "", "0"):
                            best_ask[t] = float(ba)
                        elif ba is not None:
                            best_ask[t] = None
                elif et == "last_trade_price" and slug in watch_slugs and ts:
                    prints.append((r, float(ts), e.get("asset_id"), float(e["price"]), float(e["size"])))
            # reaction probes after this message
            keep = []
            for p in pend:
                if p["slug"] == slug:
                    a = best_ask.get(p["tok"])
                    if a is not None:
                        if p["up1"] is None and a > p["a0"] + 1e-9:
                            p["up1"] = r - p["t0"]
                        if p["up2"] is None and a >= p["a0"] + 0.02 - 1e-9:
                            p["up2"] = r - p["t0"]
                if (p["up2"] is not None and len(p["at"]) == len(PROBE_MS)) or r - p["t0"] > REACT_MAX_MS:
                    react.append(p)
                else:
                    keep.append(p)
            pend = keep
            # FAK orders on this window: when did the asks at their limit vanish?
            if fak_live:
                still = []
                for o in fak_live:
                    if o["slug"] == slug and o["vanish"] is None:
                        a = best_ask.get(o["token"])
                        if a is None or a > o["limit"] + 1e-9:
                            o["vanish"] = r - o["t_send"]
                    if r > o["t_resp"] + 3000:
                        fak_done.append(o)
                    else:
                        still.append(o)
                fak_live = still
    fak_done += fak_live
    by_hour = {k: (h.quantile(0.5), h.quantile(0.99), h.n) for k, h in H.items()
               if k in ("bf.age_trade", "pm5.age", "cb.age_event", "kr.age_event", "cl.age_publish")}
    return hour, dict(H), react, fak_done, prints, by_hour


# ------------------------------------------------------------------ inputs
def token_map(slugs):
    out = {}
    slugs = sorted(slugs)
    for i in range(0, len(slugs), 20):
        try:
            js = requests.get(f"{GAMMA}/events", params=[("slug", s) for s in slugs[i:i + 20]], timeout=20).json()
        except Exception as e:
            print("gamma", e, flush=True)
            continue
        for e in js:                                       # /events also returns closed windows (/markets doesn't)
            m = (e.get("markets") or [{}])[0]
            oc = [str(x).lower() for x in json.loads(m.get("outcomes") or "[]")]
            ids = json.loads(m.get("clobTokenIds") or "[]")
            if len(oc) == 2 and len(ids) == 2:
                out[e["slug"]] = dict(zip(oc, ids))
    return out


def load_faks(db):
    rows = db.execute("select ts, origin, slug, token_id, outcome, status, latency_ms, data, error from live_orders "
                      "where order_type='market' and origin in ('fast-value-live','spikebot-live','manual')").fetchall()
    out = []
    for ts, origin, slug, tok, outcome, status, lat, data, err in rows:
        d = json.loads(data or "{}")
        req = d.get("req") or {}
        if not lat or req.get("price") is None or not tok:
            continue
        out.append({"origin": origin, "slug": slug, "token": tok, "side": outcome, "status": status,
                    "limit": float(req["price"]), "t_resp": ts * 1000, "t_send": ts * 1000 - lat, "rtt": lat,
                    "sign_ms": d.get("sign_ms"), "post_ms": d.get("post_ms"),
                    "killed": bool(err and "no orders found" in err)})
    return out


def load_maker():
    """Post-only orders and fills of the live imb_maker bots, from their window logs (exchange-acked)."""
    orders, fills = [], []
    for bot in MAKER_BOTS:
        for f in sorted((BOTS_DIR / bot / "windows").glob("*/*.json.gz")):
            try:
                w = json.load(gzip.open(f))
            except Exception:
                continue
            L = w.get("live") or {}
            oc = L.get("orders") or {}
            cols = oc.get("cols") or []
            for row in oc.get("rows") or []:
                o = dict(zip(cols, row))
                if str(o.get("oid") or "").startswith("dry"):
                    continue
                o["bot"], o["slug"] = bot, w["slug"]
                orders.append(o)
            fc = L.get("fills") or {}
            for row in fc.get("rows") or []:
                x = dict(zip(fc["cols"], row))
                if str(x.get("oid") or "").startswith("dry"):
                    continue
                x["bot"], x["slug"] = bot, w["slug"]
                fills.append(x)
    return orders, fills


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=None, help="UTC 'YYYY-MM-DD HH:MM' (default: first recording)")
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    first, lastt = replay.recording_span()
    t0 = replay._ts(a.start) if a.start else first
    t1 = replay._ts(a.end) if a.end else lastt
    files = []
    t = int(t0) // 3600 * 3600
    while t < t1:
        p = Path(RECORDINGS_DIR) / time.strftime("%Y-%m-%d/%H.tsv.gz", time.gmtime(t))
        if p.exists():
            files.append((t, p))
        t += 3600
    print(f"{len(files)} hour files {time.strftime('%m-%d %H:%M', time.gmtime(t0))} .. "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(t1))}", flush=True)

    db = sqlite3.connect(DB_PATH)
    faks = load_faks(db)
    m_orders, m_fills = load_maker()
    slugs = {f"btc-updown-5m-{s}" for s in range(int(t0) // 300 * 300, int(t1) + 300, 300)}
    toks = token_map(slugs)
    print(f"tokens for {len(toks)} windows, {len(faks)} FAK orders, {len(m_orders)} maker orders, "
          f"{len(m_fills)} maker fills", flush=True)
    watch = {x["slug"] for x in m_fills}
    jobs = []
    for ht, p in files:
        fk = [dict(o) for o in faks if ht * 1000 <= o["t_send"] < (ht + 3600) * 1000 - 3000]
        jobs.append((p, toks, fk, watch))

    H = defaultdict(Hist)
    react, fak_done, prints, by_hour = [], [], [], {}
    t_start = time.time()
    with Pool(a.workers) as pool:
        for k, (hour, h, rc, fd, pr, bh) in enumerate(pool.imap_unordered(hour_pass, jobs), 1):
            for lab, x in h.items():
                H[lab].merge(x)
            react += rc
            fak_done += fd
            prints += pr
            by_hour[hour] = bh
            print(f"{k}/{len(jobs)} {hour} done ({time.time() - t_start:.0f} s)", flush=True)

    res = {"generated": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "from": t0, "to": t1,
           "hours": len(files), "feeds": {k: H[k].summary() for k in sorted(H)}}
    worst = {}
    for lab in ("bf.age_trade", "pm5.age", "cb.age_event", "kr.age_event", "cl.age_publish"):
        rows = sorted(((hr, v[lab]) for hr, v in by_hour.items() if lab in v and v[lab][0] is not None),
                      key=lambda x: -x[1][1])
        worst[lab] = [{"hour": hr, "p50": round(v[0], 1), "p99": round(v[1], 1), "n": v[2]} for hr, v in rows[:5]]
        p50s = sorted(v[0] for _, v in rows)
        worst[lab + ".hourly_p50_range"] = [round(p50s[0], 1), round(p50s[-1], 1)] if p50s else None
    res["feeds_worst_hours"] = worst

    # reaction of Polymarket's makers after a Binance futures move
    rx = {}
    for th in TRIGGERS:
        rs = [p for p in react if p["th"] == th]
        cap = float(REACT_MAX_MS)
        rx[f"{th:g}bps"] = {
            "signals": len(rs),
            "ask_up_1tick_ms": exact([p["up1"] if p["up1"] is not None else cap for p in rs]),
            "ask_up_2c_ms": exact([p["up2"] if p["up2"] is not None else cap for p in rs]),
            "no_reprice_within_10s": round(sum(p["up1"] is None for p in rs) / max(1, len(rs)), 3),
            "ask_change_c": {f"{d}ms": exact([(p["at"][d] - p["a0"]) * 100 for p in rs
                                              if p["at"].get(d) is not None], False) for d in PROBE_MS},
            "ask_unchanged_frac": {f"{d}ms": round(sum(1 for p in rs if p["at"].get(d) is not None and
                                                       p["at"][d] <= p["a0"] + 1e-9) /
                                                   max(1, sum(1 for p in rs if p["at"].get(d) is not None)), 3)
                                   for d in PROBE_MS},
        }
    res["reaction"] = rx

    # real FAK orders
    fk = [o for o in fak_done if "ask_at_send" in o]
    gaps = [o["rtt"] - o["vanish"] for o in fk if o["vanish"] is not None and o["vanish"] <= o["rtt"] + 3000]
    res["fak"] = {
        "orders": len(faks), "matched_to_recordings": len(fk),
        "filled_frac": round(sum(1 for o in faks if o["status"] == "matched") / max(1, len(faks)), 3),
        "round_trip_ms": exact([o["rtt"] for o in faks]),
        "round_trip_ms_filled": exact([o["rtt"] for o in faks if o["status"] == "matched"]),
        "round_trip_ms_killed": exact([o["rtt"] for o in faks if o["killed"]]),
        "sign_ms": exact([o["sign_ms"] for o in faks if o["sign_ms"] is not None], False),
        "asks_at_limit_when_sent_frac": round(sum(1 for o in fk if o["vanish"] is None or o["vanish"] > 0) /
                                              max(1, len(fk)), 3),
        "asks_vanish_after_send_ms": exact([o["vanish"] for o in fk if o["vanish"]]),
        "asks_survive_past_response_frac": round(sum(1 for o in fk if o["vanish"] is None or
                                                     o["vanish"] > o["rtt"]) / max(1, len(fk)), 3),
        "queue_gap_ms (response - asks vanished)": exact(gaps),
        "queue_gap_ms_killed": exact([o["rtt"] - o["vanish"] for o in fk if o["killed"] and o["vanish"] is not None
                                      and o["vanish"] <= o["rtt"]]),
        # per order, for model checks: [round trip ms, asks at the limit vanished ms after send (0: none at send,
        # null: still there 3 s after the response), filled 1/0, ask at send, limit, origin]
        "rows": [[round(o["rtt"], 1), None if o["vanish"] is None else round(o["vanish"], 1),
                  int(o["status"] == "matched"), o["ask_at_send"], o["limit"], o["origin"]] for o in fk],
        "by_origin": {org: {"orders": sum(o["origin"] == org for o in faks),
                            "filled": sum(o["origin"] == org and o["status"] == "matched" for o in faks)}
                      for org in sorted({o["origin"] for o in faks})},
    }

    # post-only orders and maker fills
    def dl(o, a_, b_):                                     # <= 0.5 ms: answered locally, not by the exchange
        return (o[b_] - o[a_]) if o.get(a_) and o.get(b_) and o[b_] - o[a_] > 0.5 else None
    acked = [o for o in m_orders if dl(o, "sent_ms", "ack_ms") is not None]
    res["maker"] = {
        "orders": len(m_orders),
        "status": {s: sum(1 for o in m_orders if o.get("status") == s) for s in sorted({str(o.get("status"))
                                                                                       for o in m_orders})},
        "place_ack_ms": exact([dl(o, "sent_ms", "ack_ms") for o in acked]),
        "place_post_ms": exact([o.get("post_ms") for o in m_orders if o.get("post_ms")]),
        "cancel_ack_ms": exact([dl(o, "cancel_ms", "cancel_ack_ms") for o in m_orders]),
        "by_bot": {b: {"place_ack_ms": exact([dl(o, "sent_ms", "ack_ms") for o in acked if o["bot"] == b], False),
                       "cancel_ack_ms": exact([dl(o, "cancel_ms", "cancel_ack_ms") for o in m_orders
                                               if o["bot"] == b], False)} for b in MAKER_BOTS},
    }
    # fill notice: our user-channel receipt - the public print of the same trade. The print is on our token at our
    # price, or (95 % of maker fills: the taker bought the other outcome) on the other token at 1 - our price; the
    # latest such print at or before the notice, within 10 s
    pr_by = defaultdict(list)
    for rcv, ets, tok, px, sz in prints:
        pr_by[(tok, round(px, 4))].append((ets, rcv))
    for v in pr_by.values():
        v.sort()
    eng, pub, cross = [], [], 0
    for x in m_fills:
        tm = toks.get(x["slug"], {})
        tk = tm.get(x["side"])
        other = tm.get("down" if x["side"] == "up" else "up")
        px = float(x["price"])
        if not x.get("ts_ms"):
            continue
        best = None
        for key, is_cross in (((tk, round(px, 4)), False), ((other, round(1 - px, 4)), True)):
            lst = pr_by.get(key) or []
            i = bisect.bisect_right(lst, (x["ts_ms"], math.inf)) - 1
            if i >= 0 and x["ts_ms"] - lst[i][0] <= 10000 and (best is None or lst[i][0] > best[0][0]):
                best = (lst[i], is_cross)
        if best:
            eng.append(x["ts_ms"] - best[0][0])
            pub.append(x["ts_ms"] - best[0][1])
            cross += best[1]
    res["maker"]["fill_prints_cross_token"] = cross
    res["maker"]["fills"] = len(m_fills)
    res["maker"]["fill_notice_after_engine_ms"] = exact(eng)
    res["maker"]["fill_notice_after_public_print_ms"] = exact(pub)

    path = OUT_DIR / f"history-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    path.write_text(json.dumps(res, indent=1))
    print(f"wrote {path}", flush=True)
    for k in ("bf.age_trade", "pm5.age", "cb.age_event", "kr.age_event", "cl.age_publish"):
        s = res["feeds"].get(k, {})
        print(f"{k:18s} n {s.get('n')} p50 {s.get('p50')} p99 {s.get('p99')} max {s.get('max')}")
    for th, v in rx.items():
        print(th, v["signals"], "ask up 1 tick p50", v["ask_up_1tick_ms"].get("p50"), "unchanged",
              v["ask_unchanged_frac"])
    f = res["fak"]
    print("fak", f["orders"], "rtt p50", f["round_trip_ms"].get("p50"), "gap p50",
          f["queue_gap_ms (response - asks vanished)"].get("p50"), "survive", f["asks_survive_past_response_frac"])
    m = res["maker"]
    print("maker place ack p50", m["place_ack_ms"].get("p50"), "cancel ack p50", m["cancel_ack_ms"].get("p50"),
          "fill notice p50", m["fill_notice_after_engine_ms"].get("p50"))


if __name__ == "__main__":
    main()

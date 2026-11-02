"""Build backend/bots/latency_profile.json (config.LATENCY_PROFILE) from the newest measurements in data/latency/:

    .venv\\Scripts\\python -m scripts.latency_build

  probe-<site>-*.json   (scripts/latency_probe.py; newest per site)  -> sites.<site>: net.*, feed.*, gap.*, ping.*
  history-*.json        (scripts/latency_history.py on the server)   -> sites.dublin: bot_feed.*, bot_gap.*;
                                                                        shared: order.*, market.*
  orders-<site>-*.json  (scripts/order_speed_probe.py --site; newest)  -> sites.<site>: order.post_only.ack /
                        .on_book, order.cancel.ack / .off_book (override the shared keys at that site)
(data/latency/aztest/: the 2026-11-01 London AZ comparison; not read)
It also checks the taker models against every real FAK order (history rows): for each model, the fill rate it
predicts and how often it agrees with what the order really did; the result is stored under "validation" and
the best model is what Latency.fak_match_ms implements. Keys and meaning: docs/latency/README.md.
"""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import LATENCY_DIR, LATENCY_PROFILE  # noqa: E402

PCTS = ("min", "p1", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "p99", "p99.9", "max")
PCT_AT = {"min": 0, "p1": 1, "p5": 5, "p10": 10, "p25": 25, "p50": 50, "p75": 75, "p90": 90, "p95": 95, "p99": 99,
          "p99.9": 99.9, "max": 100}
KEEP = ("n", "mean") + PCTS


def dist(s, unit="ms", note=None):
    """A profile entry from a summary; q synthesised from the percentiles when the summary has none."""
    if not s or not s.get("n"):
        return None
    out = {k: s[k] for k in KEEP if s.get(k) is not None}
    q = s.get("q")
    if not q:
        pts = [(PCT_AT[k], s[k]) for k in PCTS if s.get(k) is not None]
        q = []
        for i in range(101):
            for (a, va), (b, vb) in zip(pts, pts[1:]):
                if a <= i <= b:
                    q.append(round(va + (vb - va) * ((i - a) / (b - a) if b > a else 0), 3))
                    break
        out["q_from"] = "percentiles"
    out["q"] = q
    out["unit"] = unit
    if note:
        out["note"] = note
    return out


def newest(pattern):
    fs = sorted(LATENCY_DIR.glob(pattern))
    return fs[-1] if fs else None


FEED_LABEL = {   # probe feed -> the age that matters (venue event -> us), then the publish-time age
    "binance_fut_trade": ("trade", "event"), "binance_fut_book": ("trade", "event"),
    "binance_spot_trade": ("trade", "event"), "coinbase": ("event",), "kraken": ("event",),
    "kraken_trade": ("trade",), "chainlink": ("event", "observation"), "polymarket_book": ("event",),
}
BOT_FEEDS = {    # recorder history label -> profile key
    "bf.age_trade": "bot_feed.binance_fut_trade", "bf.age_event": "bot_feed.binance_fut_trade.publish",
    "bf.venue_E_minus_T": "venue.binance_fut_aggtrade_publish_after_trade",
    "cb.age_event": "bot_feed.coinbase", "kr.age_event": "bot_feed.kraken",
    "cl.age_publish": "bot_feed.chainlink", "cl.age_observation": "bot_feed.chainlink.observation",
    "pm5.age": "bot_feed.polymarket_book", "pm5.age.price_change": "bot_feed.polymarket_book.price_change",
    "pm5.age.book": "bot_feed.polymarket_book.snapshot",
    "pm5.age.last_trade_price": "bot_feed.polymarket_book.trade_print", "pm15.age": "bot_feed.polymarket_book_15m",
    "bf.gap": "bot_gap.binance_fut_trade", "bn.gap": "bot_gap.binance_spot_book", "cb.gap": "bot_gap.coinbase",
    "kr.gap": "bot_gap.kraken", "cl.gap": "bot_gap.chainlink", "pm5.gap": "bot_gap.polymarket_book",
}


def build_site(probe):
    s = {}
    for host, r in (probe.get("network") or {}).items():
        for k, lab in (("tcp_connect", "tcp"), ("tls_handshake", "tls"), ("http_keepalive", "http")):
            d = dist(r.get(k))
            if d:
                s[f"net.{host}.{lab}"] = d
    for name, f in (probe.get("feeds") or {}).items():
        if "age" not in f:
            continue
        labs = FEED_LABEL.get(name, ())
        for i, lab in enumerate(labs):
            d = dist(f["age"].get(lab))
            if d:
                s[f"feed.{name}" if i == 0 else f"feed.{name}.{lab}"] = d
        for k, lab in (("gap", "gap"), ("ws_ping", "ping"), ("connect", "connect")):
            d = dist(f.get(k))
            if d:
                s[f"{lab}.{name}"] = d
        if f.get("msgs_per_s") is not None:
            s[f"rate.{name}"] = {"msgs_per_s": f["msgs_per_s"], "unit": "msgs/s"}
    return s


# ------------------------------------------------------------------ taker model check
def check_fak(rows, shared, seed=1, hold=None):
    """rows: [rtt, vanish (0 = no ask at the limit at send, None = asks still there 3 s after the response),
    filled, ask_at_send, limit, origin]. A model gives the match time T (ms after send); the order fills iff the
    asks at the limit are still there at T (vanish is None or vanish > T)."""
    rng = random.Random(seed)
    rows = [r for r in rows if r[1] != 0]                     # orders whose limit was already gone at send: excluded
    real = sum(r[2] for r in rows) / max(1, len(rows))
    q_all = shared["order.fak.round_trip"]["q"]
    q_fill = shared["order.fak.round_trip_filled"]["q"]

    def draw(q):
        u = rng.random() * 100
        i = int(u)
        return q[i] + (q[min(i + 1, 100)] - q[i]) * (u - i)

    def run(name, T_of):
        n, agree, pred = 0, 0, 0.0
        for r in rows:
            fills = 0
            for _ in range(200):
                T = T_of(r)
                fills += (r[1] is None or r[1] > T)
            p = fills / 200
            pred += p
            agree += p if r[2] else (1 - p)
            n += 1
        return {"model": name, "predicted_fill_rate": round(pred / n, 3), "real_fill_rate": round(real, 3),
                "agreement": round(agree / n, 3), "orders": n}
    models = [
        run("fixed 50 ms (old paper default)", lambda r: 50.0),
        run("fixed 713 ms (median FAK round trip)", lambda r: 713.0),
        run("random draw of all FAK round trips (stale_taker LATENCY_Q style)", lambda r: draw(q_all)),
        run("queue model: held until 2200 ms (fast_player queue_max_ms 2200)", lambda r: 2200.0),
        run("own real round trip (the response time; upper bound, not usable in a sim)", lambda r: r[0]),
    ]
    for H in (1000, 1500, 3000, 4000):
        models.append(run(f"held until {H} ms", lambda r, H=H: float(H)))
    if hold is not None:
        models.append(run("hold", lambda r: float(hold)))
    return models


def fak_model(rows):
    """The taker hold, from every real FAK. Event = the makers pulled the asks at the limit at V (killed orders);
    filled orders are censored at their match time (the asks were still there; their recorded 'vanish' is our own
    take). Kaplan-Meier S(t) = P(stale asks still there t ms after send). The effective hold H solves
    S(H) = the real fill rate: an order fills only if its asks outlive H."""
    rows = [r for r in rows if r[1] != 0]
    real = sum(r[2] for r in rows) / max(1, len(rows))
    obs = sorted([(r[1], 1) for r in rows if not r[2] and r[1] is not None] +
                 [(r[0], 0) for r in rows if r[2] or r[1] is None])
    surv, S, at = [], 1.0, len(obs)
    for t, e in obs:
        if e:
            S *= 1 - 1 / at
        at -= 1
        surv.append((t, S))

    def S_at(t):
        v = 1.0
        for x, sv in surv:
            if x > t:
                break
            v = sv
        return v
    hold = next((t for t, sv in surv if sv <= real), surv[-1][0])
    grid = [0, 100, 250, 500, 750, 1000, 1250, 1500, 1750, 2000, 2500, 3000, 4000, 5000]
    agree = sum(((r[1] is None or r[1] > hold) == bool(r[2])) for r in rows) / len(rows)
    return {"hold_ms": round(hold, 1), "real_fill_rate": round(real, 4), "orders": len(rows),
            "stale_ask_survival": [[t, round(S_at(t), 4)] for t in grid],
            "agreement_on_raw_rows": round(agree, 3),
            "rule": "asks at the limit vanish (in the sim's book) before hold_ms after send -> the order matches "
                    "order.fak.queue_gap after the vanish, against the book then (killed unless asks came back); "
                    "else it matches order.fak.round_trip_filled after send, against the book then",
            "unit": "ms"}


def main():
    hist_p = newest("history-*.json")
    sites, sources = {}, {}
    for p in sorted(LATENCY_DIR.glob("probe-*.json")):
        site = "-".join(p.stem.split("-")[1:-2])            # probe-<site>-<YYYYmmdd>-<HHMMSS>
        if site.endswith("test"):
            continue
        sources[site] = p                                    # sorted by name -> newest last
    prof = {"generated": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "sites": {}, "shared": {},
            "sources": {}, "validation": {}}
    for site, p in sources.items():
        probe = json.loads(p.read_text())
        prof["sites"][site] = build_site(probe)
        prof["sources"][f"probe.{site}"] = {"file": p.name, "minutes": probe.get("minutes"),
                                            "clock": probe.get("clock_start"), "load": probe.get("load_start"),
                                            "platform": probe.get("platform")}
    if hist_p:
        h = json.loads(hist_p.read_text())
        prof["sources"]["history"] = {"file": hist_p.name, "hours": h.get("hours"),
                                      "from": time.strftime("%Y-%m-%d %H:%M", time.gmtime(h["from"])),
                                      "to": time.strftime("%Y-%m-%d %H:%M", time.gmtime(h["to"]))}
        d = prof["sites"].setdefault("dublin", {})
        for lab, key in BOT_FEEDS.items():
            x = dist(h["feeds"].get(lab))
            if x:
                (prof["shared"] if key.startswith("venue.") else d)[key] = x
        d["bot_feed.worst_hours"] = {"unit": "ms", "hours": h.get("feeds_worst_hours")}
        sh = prof["shared"]
        f = h["fak"]
        for lab, key in (("round_trip_ms", "order.fak.round_trip"), ("round_trip_ms_filled", "order.fak.round_trip_filled"),
                         ("round_trip_ms_killed", "order.fak.round_trip_killed"), ("sign_ms", "order.sign"),
                         ("queue_gap_ms_killed", "order.fak.queue_gap"),
                         ("queue_gap_ms (response - asks vanished)", "order.fak.queue_gap.all_orders"),
                         ("asks_vanish_after_send_ms", "order.fak.asks_vanish_after_send")):
            x = dist(f.get(lab))
            if x:
                sh[key] = x
        sh["order.fak.facts"] = {k: f[k] for k in ("orders", "matched_to_recordings", "filled_frac",
                                                   "asks_at_limit_when_sent_frac",
                                                   "asks_survive_past_response_frac", "by_origin")}
        m = h["maker"]
        fast = m.get("by_bot", {}).get("imb-maker-10", {})      # the current fast order path = the default
        for lab, key in (("place_ack_ms", "order.post_only.ack"), ("cancel_ack_ms", "order.cancel.ack")):
            x = dist(fast.get(lab), note="imb-maker-10 (fast order path, 2026-10-27); .all = every live maker order")
            if x:
                sh[key] = x
        for lab, key in (("place_ack_ms", "order.post_only.ack.all"), ("place_post_ms", "order.post_only.post"),
                         ("cancel_ack_ms", "order.cancel.ack.all"), ("fill_notice_after_engine_ms", "order.fill_notice"),
                         ("fill_notice_after_public_print_ms", "order.fill_notice.after_public_print")):
            x = dist(m.get(lab))
            if x:
                sh[key] = x
        sh["order.post_only.facts"] = {"orders": m["orders"], "status": m["status"], "fills": m["fills"],
                                       "by_bot": m["by_bot"]}
        for th, v in h["reaction"].items():
            for lab, key in (("ask_up_1tick_ms", "reprice_1tick"), ("ask_up_2c_ms", "reprice_2c")):
                x = dist(v.get(lab), note="capped at 10000 ms = no reprice within 10 s")
                if x:
                    sh[f"market.{key}.{th}"] = x
            for dly, s in v["ask_change_c"].items():
                x = dist(s, unit="cents")
                if x:
                    sh[f"market.ask_change.{th}.{dly}"] = x
            sh[f"market.facts.{th}"] = {"signals": v["signals"], "no_reprice_within_10s": v["no_reprice_within_10s"],
                                        "ask_unchanged_frac": v["ask_unchanged_frac"]}
        if f.get("rows") and "order.fak.round_trip" in sh:
            sh["order.fak.model"] = fak_model(f["rows"])
            prof["validation"]["fak_models"] = check_fak(f["rows"], sh)
            H = sh["order.fak.model"]["hold_ms"]
            prof["validation"]["fak_models"].append(
                {"model": f"calibrated hold {H:.0f} ms (order.fak.model, what Latency.fak_match_ms does)",
                 **{k: v for k, v in check_fak(f["rows"], sh, hold=H)[-1].items() if k != "model"}})
    for site in sorted({"-".join(p.stem.split("-")[1:-2]) for p in LATENCY_DIR.glob("orders-*.json")}):
        p = newest(f"orders-{site}-*.json")                  # scripts/order_speed_probe.py --site: overrides shared
        o = json.loads(p.read_text())
        note = (f"order_speed_probe {p.stem[-15:]} at {site}: {o['accepted']} unfillable post-only orders on a "
                "not-yet-open window, calm book (the shared .ack keys come from live bots and include busy moments)")
        d = prof["sites"].setdefault(site, {})
        for lab, key in (("post_ack", "order.post_only.ack"), ("cancel_ack", "order.cancel.ack"),
                         ("on_book", "order.post_only.on_book"), ("off_book", "order.cancel.off_book")):
            x = dist(o.get(lab), note=note)
            if x:
                d[key] = x
        prof["sources"][f"orders.{site}"] = {"file": p.name, "orders": o["orders"], "accepted": o["accepted"]}
    LATENCY_PROFILE.write_text(json.dumps(prof, indent=1))
    print(f"wrote {LATENCY_PROFILE} ({LATENCY_PROFILE.stat().st_size / 1024:.0f} KB): "
          f"{ {s: len(v) for s, v in prof['sites'].items()} } site keys, {len(prof['shared'])} shared")
    fm = prof["shared"].get("order.fak.model")
    if fm:
        print("taker hold", fm["hold_ms"], "ms; stale ask survival", fm["stale_ask_survival"])
    for m in prof["validation"].get("fak_models", []):
        print(f"  {m['model']:75s} fills {m['predicted_fill_rate']:.3f} (real {m['real_fill_rate']:.3f}) "
              f"agree {m['agreement']:.3f}")


if __name__ == "__main__":
    main()

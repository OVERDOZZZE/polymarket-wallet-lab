"""Measured delays for simulations: every paper bot, replay and backtest takes its delays from here instead of
guessing a fixed latency_ms.

The numbers live in backend/bots/latency_profile.json (config.LATENCY_PROFILE), built by scripts/latency_build.py
from real measurements (scripts/latency_probe.py, scripts/latency_history.py; how and what: docs/latency/README.md).
Each entry is a distribution in ms (or cents for ask changes): n, mean, min, p1..p99.9, max, and q = 101 quantiles
(q[0] = min, q[50] = median, q[100] = max) that draw() samples by inverse CDF, so tails come out as often as
they happened.

    from backend.bots.latency import Latency
    lat = Latency()                                    # site "dublin" (the server the bots trade from)
    lat.draw("feed.binance_fut_trade")                 # one realistic age of a Binance futures trade at the bot
    lat.pick("order.fak.round_trip", "worst")          # p99 (also: best = min, typical = p50, mean, max)
    lat.keys()                                         # everything measured
    t_match = lat.fak_match_ms(rtt_ms=None, vanish_ms=420.0)   # taker delay model (see the method)

Modes for pick(): best (min), p1, typical (p50), mean, p90, p95, worst (p99), max. A sim that is right only at
"typical" and loses at "worst" is not ready for real money.
"""
import json
import random
from functools import lru_cache

from ..config import LATENCY_PROFILE

MODES = {"best": "min", "p1": "p1", "typical": "p50", "median": "p50", "mean": "mean", "p90": "p90", "p95": "p95",
         "worst": "p99", "p99": "p99", "max": "max"}


@lru_cache(maxsize=4)
def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Latency:
    def __init__(self, site="dublin", path=None, seed=None):
        self.profile = _load(str(path or LATENCY_PROFILE))
        if site not in self.profile["sites"]:
            raise KeyError(f"no site {site!r} in the profile (have {sorted(self.profile['sites'])})")
        self.site = site
        self.rng = random.Random(seed)

    # ---------------------------------------------------------------- lookup
    def dist(self, key):
        """The distribution for `key`: the site's entry first ("feed.x", "net.x.http"), then the shared ones
        ("order.*", "market.*")."""
        d = self.profile["sites"][self.site].get(key)
        if d is None:
            d = self.profile["shared"].get(key)
        if d is None:
            raise KeyError(f"{key!r} not measured (see Latency.keys())")
        return d

    def keys(self):
        return sorted(set(self.profile["sites"][self.site]) | set(self.profile["shared"]))

    def pick(self, key, mode="typical"):
        """A fixed value: best / typical / mean / worst (p99) / max ... of `key`."""
        return self.dist(key)[MODES[mode]]

    def draw(self, key, rng=None):
        """One random value of `key`, by inverse CDF over its 101 quantiles (linear in between)."""
        q = self.dist(key)["q"]
        u = (rng or self.rng).random() * (len(q) - 1)
        i = int(u)
        j = min(i + 1, len(q) - 1)
        return q[i] + (q[j] - q[i]) * (u - i)

    # ---------------------------------------------------------------- composite models
    def signal_delay_ms(self, feed, mode=None, rng=None):
        """Venue event -> the bot has the message: the feed's age at this site (draw, or pick(mode))."""
        key = f"feed.{feed}"
        return self.pick(key, mode) if mode else self.draw(key, rng)

    @property
    def fak_hold_ms(self):
        """The calibrated taker hold: a FAK fills only if the asks at its limit outlive this (order.fak.model)."""
        return self.profile["shared"]["order.fak.model"]["hold_ms"]

    def fak_match_ms(self, vanish_ms=None, mode=None, rng=None):
        """When a FAK (taker) order really matches, ms after it was sent; match it against the sim's book AT that
        moment (fill only what is at <= limit then).

        Polymarket holds orders that take liquidity, and the makers pull their quotes during the hold: on every
        real FAK of ours, an order filled only if the asks at its limit were still there ~fak_hold_ms after it
        was sent (Kaplan-Meier on 300+ real orders: docs/latency/README.md). So:
          * vanish_ms = when, in the sim's book, nothing is left at the order's limit (ms after send; None if the
            asks stay at least fak_hold_ms);
          * vanished within the hold -> the order matches order.fak.queue_gap after the vanish (the book then is
            normally empty at the limit: killed);
          * otherwise it matches order.fak.round_trip_filled after send (the calm-book delay).
        mode=None draws (use for replays/paper); mode="typical"/"worst"/... gives fixed values for stress tests.
        """
        r = rng or self.rng
        f = (lambda k: self.pick(k, mode)) if mode else (lambda k: self.draw(k, r))
        if vanish_ms is not None and vanish_ms < self.fak_hold_ms:
            return vanish_ms + max(0.0, f("order.fak.queue_gap"))
        return f("order.fak.round_trip_filled")

    def maker_times(self, mode=None, rng=None):
        """(place ack, cancel ack, fill notice) ms for a post-only order: when the exchange has it, when a cancel
        has taken effect, when we learn of a fill after the engine matched it."""
        r = rng or self.rng
        f = (lambda k: self.pick(k, mode)) if mode else (lambda k: self.draw(k, r))
        return f("order.post_only.ack"), f("order.cancel.ack"), f("order.fill_notice")

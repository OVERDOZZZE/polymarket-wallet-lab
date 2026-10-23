"""Bot strategies and bot records.

A *strategy* is code (backend/bots/strategies/<name>.py) exposing an engine class and a config dataclass.
A *bot* is a DB row (table `bots`) = strategy + config + mode + paper account. Many bots can share a strategy.

To add a strategy: write the module, then add one entry to STRATEGIES. The engine class must accept
`bot_id`, and provide start(), stop(), snapshot(), running, publish_state, _thread (see market_maker.PaperBot).
"""
import importlib
import re
import time

from .. import db

STRATEGIES = {
    "market_maker": {
        "module": "backend.bots.strategies.market_maker", "engine": "PaperBot", "config": "BotConfig",
        "label": "Rebate market maker",
        "description": "Two-sided resting bids on BTC 5m/15m Up/Down windows, held to settlement for maker "
                       "rebates (CrouchPredictions reconstruction).",
    },
    "ml_maker": {
        "module": "backend.bots.strategies.ml_maker", "engine": "MLBot", "config": "MLConfig",
        "label": "machinelearned reconstruction",
        "description": "BTC 5m: joins the best bid on both sides, capped at fair value; improves early, bids "
                       "below late, small cheap-side tickets and model-driven takes. Sized as a % of its orders.",
    },
    "join_copy": {
        "module": "backend.bots.strategies.join_copy", "engine": "JoinCopyBot", "config": "JoinConfig",
        "label": "Live bid copier",
        "description": "Watches a wallet live; after each of its fills, bids at the same price on the same outcome "
                       "and holds to resolution. Sized as a % of each fill.",
    },
    "crouch_v2": {
        "module": "backend.bots.strategies.crouch_v2", "engine": "CrouchBot", "config": "CrouchConfig",
        "label": "CrouchPredictions v2",
        "description": "BTC 5m/15m: one bid per side at the best bid (capped at fair), sized by price (large at the "
                       "extremes), bids 1-99c, trades to the close, plus 20/30-share momentum takes. Holds to "
                       "settlement. Calibrated on its live fills of 2026-10-21.",
    },
    "fast_player": {
        "module": "backend.bots.strategies.fast_player", "engine": "FastBot", "config": "FastConfig",
        "label": "Fast player",
        "description": "BTC 5m latency taker: when Coinbase/Kraken/Binance jump, buys that side before Polymarket's "
                       "book reprices (FAK at a limit that keeps the edge over fair value), then holds to settlement "
                       "or scalps the repricing back into the bid. Rebuilt from FAST_PLAYER_SPEC.md for Dublin.",
    },
    "late_fav": {
        "module": "backend.bots.strategies.late_fav", "engine": "LateFavBot", "config": "LateFavConfig",
        "label": "Late-favourite maker",
        "description": "BTC 5m: in the last 2 minutes, resting bids at 97-99c on the near-certain side, held to "
                       "settlement; all bids pulled on a fast BTC move. From the old PC's 60 h study (+0.7-1.7% per $1 "
                       "on every wallet's maker fills there).",
    },
    "guard_maker": {
        "module": "backend.bots.strategies.guard_maker", "engine": "GuardMakerBot", "config": "GuardMakerConfig",
        "label": "Guarded two-sided maker",
        "description": "BTC 5m/15m: post-only bids on Up and Down just below fair value for the spread and the maker "
                       "rebate. When Coinbase/Kraken/Binance spike, the side about to be picked off is cancelled at "
                       "once (our cancels land in ~30 ms; takers are held ~250-500 ms). Holds to settlement.",
    },
}
MODES = ("paper", "live")     # live: fast_player only (real buys next to its paper twin; see fast_player.py)


def strategy(name):
    s = STRATEGIES[name]
    mod = importlib.import_module(s["module"])
    return getattr(mod, s["engine"]), getattr(mod, s["config"])


def default_config(name):
    return strategy(name)[1]().to_dict()


def list_bots():
    out = []
    for r in db.q("SELECT * FROM bots ORDER BY created"):
        r["config"] = db.loads(r["config"], {})
        out.append(r)
    return out


def get_bot(bot_id):
    r = db.one("SELECT * FROM bots WHERE id = ?", (bot_id,))
    if not r:
        raise KeyError(bot_id)
    r["config"] = db.loads(r["config"], {})
    return r


def create_bot(name, strategy_name="market_maker", config=None, mode="paper", bot_id=None):
    if strategy_name not in STRATEGIES:
        raise ValueError(f"unknown strategy '{strategy_name}'")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cfg_cls = strategy(strategy_name)[1]
    cfg = cfg_cls.from_dict({**cfg_cls().to_dict(), **(config or {})}).to_dict()
    base = bot_id or (re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "bot")
    bid, i = base, 2
    while db.one("SELECT id FROM bots WHERE id = ?", (bid,)):
        bid, i = f"{base}-{i}", i + 1
    now = time.time()
    db.x("INSERT INTO bots (id, name, strategy, mode, config, account, account_since, bankroll, created) "
         "VALUES (?,?,?,?,?,1,?,?,?)", (bid, name, strategy_name, mode, db.dumps(cfg), now,
                                        float(cfg.get("bankroll", 1000.0)), now))
    s = STRATEGIES[strategy_name]
    add_change(bid, f"Created with strategy {s['label']}: {s['description']}", "created", ts=now)
    return get_bot(bid)


def add_change(bot_id, text, kind="note", ts=None):
    """One entry in the bot's history (shown at the bottom of its page): created, rebuilt, audited,
    simulator changes, setting changes, new accounts, notes."""
    db.x("INSERT INTO bot_changes (bot_id, ts, kind, text) VALUES (?,?,?,?)",
         (bot_id, ts if ts is not None else time.time(), kind, text))


def changes(bot_id):
    return db.q("SELECT id, ts, kind, text FROM bot_changes WHERE bot_id = ? ORDER BY ts DESC, id DESC", (bot_id,))


def update_bot(bot_id, **fields):
    allowed = {"name", "notes", "mode"}
    for k, v in fields.items():
        if k not in allowed:
            raise KeyError(k)
        db.x(f"UPDATE bots SET {k} = ? WHERE id = ?", (v, bot_id))
    return get_bot(bot_id)


def delete_bot(bot_id):
    for t in ("bot_fills", "bot_windows", "bot_equity", "bot_changes"):
        db.x(f"DELETE FROM {t} WHERE bot_id = ?", (bot_id,))
    db.x("DELETE FROM bots WHERE id = ?", (bot_id,))

# Bots — versions, notes and results

One folder per **strategy** (the code in `backend/bots/strategies/<strategy>.py`). Every bot built on that
strategy is a **version** listed in the folder's `README.md`.

| Folder | Strategy | Bots |
|---|---|---|
| [market_maker/](market_maker/) | Rebate market maker | `crouch-mm` |
| [ml_maker/](ml_maker/) | machinelearned reconstruction | `ml-copy` |
| [join_copy/](join_copy/) | Live bid copier | `ml-join` (deleted) |
| [crouch_v2/](crouch_v2/) | CrouchPredictions v2 | `crouch-v2` |
| [fast_player/](fast_player/) | Fast player (latency taker) | `fast-hold`, `fast-scalp`, `fast-value`, `*-v2`, `fast-value-live` |
| [late_fav/](late_fav/) | Late-favourite maker | `late-fav`, `late-fav-v2*`, `late-fav-v3*` |
| [guard_maker/](guard_maker/) | Guarded two-sided maker | `guard-mm-5m`, `guard-mm-15m` (deleted) |
| [bot_sim/](bot_sim/) | Imbalance maker (`imb_maker`) | `imb-maker`, `imb-maker-300` |
| [stale_taker/](stale_taker/) | Stale-quote taker | `stale-taker-1bps`, `stale-taker-2bps` |

## Layout of a strategy folder

```
<strategy>/
  README.md            what the strategy does, versions table, one section per version
  <SPEC>.md            optional: specs, design notes, studies the strategy came from
  results/             small outputs worth keeping: exported fills/windows CSVs, PnL summaries, charts,
                       replay results. Name files <bot-id>_<what>_<YYYY-MM-DD>.<ext>
```

Bulky or live data stays where the app writes it, so don't copy it here:
* DB `data/btc5m.db`: tables `bots`, `bot_fills`, `bot_windows`, `bot_equity`, `bot_changes`, `live_orders`
  (rows keyed by `bot_id` + `account`)
* `data/bots/<id>/` (state.json, bot.log), `data/recordings/` (replay input)
* Home PC and Dublin server have **separate** databases. Say which one a result came from.

## Template for a version section

```markdown
### <bot-id> — <one-line summary>
* Created: YYYY-MM-DD · Where: home / Dublin · Mode: paper / live · Status: running / stopped / deleted
* Config: what differs from the strategy defaults (or the previous version)
* Why: the idea behind this version
* Results: period, account #, PnL, fills, win rate (link files in results/)
* Verdict / next:
```

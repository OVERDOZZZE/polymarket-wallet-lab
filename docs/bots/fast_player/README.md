# Fast player

Code: `backend/bots/strategies/fast_player.py` · Strategy id: `fast_player`

## What it does

BTC 5m latency taker: when Coinbase/Kraken/Binance jump, FAK-buys that side before Polymarket's book reprices, then holds to settlement or scalps the repricing back into the bid.

Signal feeds (`signal_feeds`, a dropdown in Settings, applied to the running bot at once): `binance_fut` = Binance
USD-M futures only; `classic` = mean move of Coinbase + Kraken + Binance spot; `custom` (the default for older bots) =
the config's own `sources` + `combine`. Study 2026-11-02 (`scripts/source_leadlag.py`, `scripts/fast_source_replay.py`):
Binance futures leads the other feeds even as received in London (~30 ms ahead of Coinbase); on the queue fill model
futures-only made +$65 / +$42 (London 11-01 / Dublin 10-24..25, 17 fills) vs classic -$35 / +$2 (14 fills). Thin.

Background: [FAST_PLAYER_SPEC.md](FAST_PLAYER_SPEC.md) (the earlier btc5m bots it was rebuilt from),
`reports/ult_fastbots.txt`, `reports/ult_reaction.txt`, `reports/latency_result.txt`.

## Versions

| Bot id | Created | Where | Mode | Status | Summary |
|---|---|---|---|---|---|
| `fast-hold` | | Dublin | paper | | |
| `fast-scalp` | | Dublin | paper | | |
| `fast-value` | | Dublin | paper | | |
| `fast-hold-v2` | | Dublin | paper | | |
| `fast-scalp-v2` | | Dublin | paper | | |
| `fast-value-v2` | | Dublin | paper | | |
| `fast-value-live` | 2026-10-23 | Dublin | live | | fast-value's settings, real FAK buys (scripts/fast_value_live.py) |
| `fast-london` | 2026-11-01 | London | live | | fast-value-live's signal, $1-2 FAKs; `signal_feeds` binance_fut by default (scripts/fast_london_live.py `signal binance_fut|classic`) |

### fast-hold

### fast-scalp

### fast-value

### fast-hold-v2

### fast-scalp-v2

### fast-value-v2

### fast-value-live


# Fast player

Code: `backend/bots/strategies/fast_player.py` · Strategy id: `fast_player`

## What it does

BTC 5m latency taker: when Coinbase/Kraken/Binance jump, FAK-buys that side before Polymarket's book reprices, then holds to settlement or scalps the repricing back into the bid.

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

### fast-hold

### fast-scalp

### fast-value

### fast-hold-v2

### fast-scalp-v2

### fast-value-v2

### fast-value-live


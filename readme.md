# Polymarket Copy Trader

A bot that tracks one or more Polymarket wallets and mirrors their trades into your own account. Built with [pmxt](https://github.com/pmxt-dev/pmxt).

## How it works

1. Polls tracked wallets for position changes every second.
2. Queues detected buys and sells for a configurable **batch window** (default 2 minutes). Opposing moves on the same asset cancel out before execution.
3. At the end of each window, executes the net trade for each asset.
4. Periodically redeems resolved positions on-chain.
5. Optionally posts a portfolio summary to Slack every 30 minutes.

The bot also detects and skips **Merge operations** (simultaneous YES + NO sell on the same market) so they aren't misread as directional signals.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
npm install -g pmxtjs   # required by pmxt for order execution
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your credentials:

```
POLYMARKET_PRIVATE_KEY=0x...        # EOA private key
POLYMARKET_PROXY_ADDRESS=0x...      # Gnosis Safe proxy address

POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # optional
```

### 3. Configure `config.json`

```json
{
    "wallets_to_track": ["0x..."],
    "copy_percentage": 0.025,
    "trading_enabled": true
}
```

## Configuration reference

| Key | Default | Description |
|-----|---------|-------------|
| `wallets_to_track` | `[]` | Wallet addresses to follow |
| `copy_percentage` | `1.0` | Fraction of the tracked trader's size to copy (e.g. `0.025` = 2.5%) |
| `trading_enabled` | `false` | Set to `true` to place real orders; `false` for dry-run logging only |
| `batch_window_seconds` | `180` | Seconds to accumulate changes before executing the net trade |
| `max_buy_price` | none | Skip buys where the tracked trader's entry price exceeds this (e.g. `0.93`) |
| `min_trade_usd` | `0` | Skip buys where the tracked trader's notional value is below this threshold |
| `max_position_usd` | `0` | Cap your total cost basis in any single asset (0 = no cap) |
| `rate_limit` | `25` | Max Polymarket API requests per 10-second window |
| `redeem_interval_cycles` | `60` | Redeem resolved positions every N poll cycles |

### Low-probability position controls

Outcomes priced below `low_prob_price_threshold` are treated as a separate tier with their own sizing and risk limits.

| Key | Default | Description |
|-----|---------|-------------|
| `low_prob_price_threshold` | `0.30` | Price below which an outcome is considered "low probability" |
| `low_prob_copy_percentage` | same as `copy_percentage` | Copy fraction to use for low-prob buys |
| `low_prob_min_trade_usd` | `0` | Minimum tracked-trader notional for low-prob buys |
| `low_prob_max_portfolio_pct` | `1.0` | Max fraction of your USDC balance that can be in low-prob positions (e.g. `0.05` = 5%) |

## Running

### Directly

```bash
python -m src.main
```

### Docker

```bash
docker compose up -d
```

`config.json` is mounted as a volume — edit it and restart the container to apply changes without rebuilding the image.

```bash
docker compose restart
docker compose logs -f
```

## Slack notifications

Set `SLACK_WEBHOOK_URL` in `.env` to receive a portfolio summary every 30 minutes showing current value, unrealised and realised P&L, open position count, and the top 5 positions by value.

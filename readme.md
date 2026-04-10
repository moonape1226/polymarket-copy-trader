# Polymarket Copy Trader

A bot that tracks one or more Polymarket wallets and mirrors their trades into your own account. Built with [pmxt](https://github.com/pmxt-dev/pmxt).

Runs as three Docker services sharing a local `data/` volume:

| Service | Purpose |
|---|---|
| `copy-trader` | Polls target wallets, detects trades, executes copies |
| `market-scanner` | Scans for high-probability markets every 6h, posts to Slack |
| `trade-logger` | Logs raw activity for observed wallets to per-wallet CSVs |

## How it works

1. Polls tracked wallets for position changes every second.
2. Queues detected buys and sells for a configurable **batch window** (default 3 minutes). Opposing moves on the same asset cancel out before execution.
3. At the end of each window, executes the net trade for each asset.
4. Periodically redeems resolved positions on-chain.
5. Optionally posts a portfolio summary to Slack every 30 minutes.

The bot also detects and skips:
- **Merge operations** — simultaneous YES + NO sell on the same market, not a directional signal
- **Splits** — simultaneous YES + NO buy at ~$0.50 with equal size (1 USDC = 1 YES + 1 NO)

Small orders below the Polymarket $1 minimum are carried forward up to 5 minutes, then discarded.

## Setup

### 1. Configure environment

Copy `.env.example` to `.env` and fill in your credentials:

```
POLYMARKET_PRIVATE_KEY=0x...        # EOA private key
POLYMARKET_PROXY_ADDRESS=0x...      # Gnosis Safe proxy address

POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # optional
```

### 2. Configure `config.json`

```json
{
    "wallets_to_track": ["0x..."],
    "copy_percentage": 0.5,
    "trading_enabled": true
}
```

### 3. Run

```bash
docker compose up -d --build
docker compose logs -f copy-trader
```

`config.json` is mounted as a volume — edit it and run `./restart.sh` to apply changes without rebuilding.

### Test credentials

```bash
docker compose exec copy-trader python3 test_auth.py
```

## Configuration reference

| Key | Default | Description |
|-----|---------|-------------|
| `wallets_to_track` | `[]` | Wallet addresses the bot actively copies |
| `wallets_to_observe` | `{}` | `{name: address}` map logged by trade-logger (no copying) |
| `copy_percentage` | `1.0` | Fraction of the tracked trader's size to copy (e.g. `0.5` = 50%) |
| `trading_enabled` | `false` | Set to `true` to place real orders; `false` for dry-run logging only |
| `batch_window_seconds` | `180` | Seconds to accumulate changes before executing the net trade |
| `max_buy_price` | none | Skip buys where the tracked trader's entry price exceeds this (e.g. `0.93`) |
| `min_trade_usd` | `0` | Skip buys where the tracked trader's notional value is below this threshold |
| `max_position_usd` | `0` | Cap your total cost basis in any single asset (0 = no cap) |
| `blocked_title_keywords` | `[]` | Skip buys in markets whose title contains any of these strings |
| `rate_limit` | `25` | Max Polymarket API requests per 10-second window |
| `redeem_interval_cycles` | `60` | Redeem resolved positions every N poll cycles |

### Low-probability position controls

Outcomes priced below `low_prob_price_threshold` are treated as a separate tier with their own sizing and risk limits.

| Key | Default | Description |
|-----|---------|-------------|
| `low_prob_price_threshold` | `0.30` | Price below which an outcome is considered "low probability" |
| `low_prob_copy_percentage` | same as `copy_percentage` | Copy fraction to use for low-prob buys |
| `low_prob_min_trade_usd` | `0` | Minimum tracked-trader notional for low-prob buys |
| `low_prob_max_portfolio_pct` | `1.0` | Max fraction of total portfolio that can be in low-prob positions (e.g. `0.10` = 10%) |

## Monitoring

```bash
# Live logs
docker compose logs -f copy-trader
docker compose logs -f polymarket-scanner
docker compose logs -f polymarket-logger

# Bot trade history
cat data/bot_trades.csv

# Per-wallet observed activity (trade-logger output)
cat data/observe_<WalletName>.csv

# Scanner calibration history
cat data/scan_history.json
```

## Slack notifications

Set `SLACK_WEBHOOK_URL` in `.env` to receive:
- A portfolio summary every 30 minutes (value, unrealised/realised P&L, top 5 positions)
- Market scanner results every 6 hours (high-probability opportunities + calibration stats)

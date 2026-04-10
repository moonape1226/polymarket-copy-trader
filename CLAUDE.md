# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
# Start all three services
docker compose up -d --build

# Restart copy-trader after config.json or .env changes (no rebuild needed)
./restart.sh

# View logs
docker compose logs -f copy-trader
docker compose logs -f polymarket-scanner
docker compose logs -f polymarket-logger

# Test auth credentials inside the container
docker compose exec copy-trader python3 test_auth.py
```

## Environment Variables

Copy `.env.example` to `.env` and fill in:
- `POLYMARKET_PRIVATE_KEY` — EOA private key (required for trading and redemption)
- `POLYMARKET_PROXY_ADDRESS` — Gnosis Safe proxy wallet address
- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` — optional, for pmxt credentials
- `SLACK_WEBHOOK_URL` — optional, enables portfolio notifications and scanner alerts

## Architecture

Three independent Docker services share the `./data/` volume:

### copy-trader (`src/`)
The main trading bot. Entry point: `src/main.py`.

Poll loop (1s interval):
1. `src/positions.py:get_user_positions` — fetches positions from `data-api.polymarket.com` for each tracked wallet
2. `src/positions.py:detect_order_changes` — diffs previous vs current positions to infer executed trades; filters Merge operations (simultaneous YES+NO sells)
3. Changes are accumulated into a `pending` dict keyed by `asset_id`, with net signed size across the batch window
4. Every `batch_window_seconds` (default 30s): flush pending → run split detection (both sides at ~$0.50, equal qty = ignore) → call `src/trading.py:TradingModule.execute_copy_trade`
5. Small orders (<$1.00 our cost) are carried forward up to `max_pending_seconds` (5 min) before being discarded
6. Every `redeem_interval_cycles` iterations: `src/redeemer.py:redeem_resolved_positions` sweeps redeemable positions on-chain via Gnosis Safe `execTransaction`
7. Every 30 minutes: `src/notifier.py:send_portfolio_update` posts a Slack summary

`TradingModule` maintains in-memory exposure tracking (`_asset_exposure`, `_asset_shares`, `_low_prob_exposure`) and refreshes from on-chain positions every 60s. Orders are placed by calling the pmxt Node.js sidecar over localhost HTTP.

### market-scanner (`scanner/scanner.py`)
Standalone service. Runs every 6 hours; queries `gamma-api.polymarket.com` for markets with 80–97% probability, ≤45 days to expiry, >$10K volume and liquidity. Posts results to Slack and tracks calibration accuracy in `/data/scan_history.json`.

### trade-logger (`tracker/tracker.py`)
Standalone service. Polls `data-api.polymarket.com/activity` every 30s for each wallet in `wallets_to_observe` (config key, separate from `wallets_to_track`). Writes per-wallet CSVs to `/data/observe_<name>.csv`.

## Key Config Fields (`config.json`)

- `wallets_to_track` — list of addresses the copy-trader actively copies (one primary wallet)
- `wallets_to_observe` — `{name: address}` map logged by trade-logger (broader watchlist)
- `copy_percentage` — fraction of target's trade size to copy (0.0–1.0)
- `low_prob_copy_percentage` / `low_prob_price_threshold` — separate copy rate for positions priced below threshold
- `low_prob_max_portfolio_pct` — caps total low-prob exposure as a fraction of total portfolio value
- `max_position_usd` — per-asset USD cap
- `min_trade_usd` — minimum target trade value to copy; `$1` Polymarket minimum is also enforced
- `max_buy_price` — skip buys where target entered above this price (avoids copying at the top)
- `blocked_title_keywords` — list of substrings; matching market titles are skipped on buy
- `trading_enabled` — `false` = dry run (logs intent, no orders placed)
- `batch_window_seconds` — flush interval for pending trades

Config is hot-reloaded on container restart without a rebuild (mounted as read-only volume).

## Trade Execution Flow

Orders go through `TradingModule._create_order`, which calls the pmxt Node.js sidecar (`pmxtjs`) running on localhost. The sidecar manages signing; the Python side sends JSON with credentials per-request.

Sells are only executed if the bot actually holds that `asset_id`; the bot checks its own positions via `poly.fetch_positions()` before placing the sell.

Exposure tracking is optimistic (updated immediately on order success) and reconciled against on-chain positions every 60 seconds via `_refresh_exposure`.

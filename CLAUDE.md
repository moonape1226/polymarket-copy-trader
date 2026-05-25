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

# Test auth credentials inside the container
docker compose exec copy-trader python3 test_auth.py
```

## Environment Variables

Copy `.env.example` to `.env` and fill in:
- `POLYMARKET_PRIVATE_KEY` — EOA private key (required for trading and redemption)
- `POLYMARKET_PROXY_ADDRESS` — Gnosis Safe proxy wallet address
- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` — optional, for pmxt credentials
- `SLACK_WEBHOOK_URL` — optional, enables portfolio notifications and scanner alerts

## Polymarket V2 (post 2026-04-28)

Polymarket cut over to CTF Exchange V2 + pUSD collateral on 2026-04-28 11:00 UTC. Anything in this codebase that touches an on-chain or signing surface targets V2:

- **Collateral token**: pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (6 decimals, 1:1 backed by USDC). `_get_pusd_balance` (trading.py) and `_fetch_pusd_balance` (notifier.py) read this. The legacy USDC.e address is no longer queried anywhere.
- **CTF Exchange V2**: standard `0xE111180000d2663C0091e4f400237545B87B996B`, neg-risk `0xe2222d279d744050d28e00520010520000310F59`. EIP-712 domain version is `"2"`; signing is done inside the pmxt Node sidecar via `@polymarket/clob-client-v2`. Pin `pmxt>=2.35.18` (Python) and `pmxtjs@^2.35.18` (npm) in Dockerfile to keep this aligned.
- **Outcome token factories**: standard `0xADa100874d00e3331D00F2007a9c336a65009718`, neg-risk `0xAdA200001000ef00D07553cEE7006808F895c6F1`. Users hold *wrapped* outcome tokens here, not raw CTF positions; redemption goes through the factory's `redeemPositions(_, _, conditionId, indexSets)` (first two args are dead, kept for legacy call-shape compatibility). `redeemer.py` selects the factory by `pos["negativeRisk"]`.
- **OrderFilled topic**: `0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee` (`OrderFilled(bytes32 orderHash, address maker, address taker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata)`). `chain_feed.py` decodes against this; `side=0` is BUY, `side=1` is SELL, `tokenId` is the outcome token.

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

In parallel, two faster signal paths feed the same dispatch gate:

- **`src/chain_feed.py`** — Polygon WS subscription to V2 `OrderFilled` on both exchanges, dedup'd by `(tx_hash, log_index)` across two RPC providers; decodes (`side`, `tokenId`, `makerAmountFilled`, `takerAmountFilled`) into a buy/sell event ~1–3s after the on-chain fill. Reconnect performs `eth_getLogs` backfill from `_last_block`.
- **`src/ws_feed.py`** — Polymarket CLOB WS for live ask/bid; used by the WS dual-signal path in `main.py` to confirm BS fills before placing copy orders.

`TradingModule` maintains in-memory exposure tracking (`_asset_exposure`, `_asset_shares`, `_low_prob_exposure`) and refreshes from on-chain positions every 60s. Orders are placed by calling the pmxt Node.js sidecar (`pmxtjs`, V2 client) over localhost HTTP; the sidecar holds the V2 EIP-712 signing keys.

### market-scanner (`scanner/scanner.py`)
Standalone service. Runs every 15 minutes (`SCAN_INTERVAL_MIN`); queries `gamma-api.polymarket.com` for markets with 80–97% probability, ≤7 days to expiry, >$5K volume, >$3K liquidity. Excludes sports/esports (tag-based + `SPORTS_TITLE_PATTERNS` title heuristics) and crypto coin-price strike markets (`looks_like_crypto_price`: a title/question with both a coin token and a price/direction hint — confirmed net-negative slice, EV −1.7% vs ex-crypto +9.3% over the 4/22–5/19 dataset; crypto-adjacent non-price markets like ETF approval are kept). Per event keeps the top 3 markets by liquidity (`MAX_MARKETS_PER_EVENT`) to limit bucket-event correlation. New/updated markets are log-only (Slack push disabled by request); existing ones only re-log when probability shifts ≥5% (`NOTIFY_PROB_DELTA`). Logs per-market, per-event, and per-topic calibration (`event_correct_stats`: a bucket event is "correct" only if all its resolved markets resolved correctly). The calibration summary still posts to Slack on newly-resolved markets; calibration tracked in `/data/scan_history.json` (30-day retention). Filter rationale and codex review in `audit/scanner_optimize_spec.md`.

## Key Config Fields (`config.json`)

- `wallets_to_track` — list of addresses the copy-trader actively copies (one primary wallet)
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

Orders go through `TradingModule._create_order`, which calls the pmxt Node.js sidecar (`pmxtjs`) running on localhost. The sidecar manages V2 EIP-712 signing (domain version `"2"`, V2 verifying contracts above); the Python side sends JSON with credentials per-request.

Sells are only executed if the bot actually holds that `asset_id`; the bot checks its own positions via `poly.fetch_positions()` before placing the sell.

Exposure tracking is optimistic (updated immediately on order success) and reconciled against on-chain positions every 60 seconds via `_refresh_exposure`.

## Redemption Flow

`src/redeemer.py:redeem_resolved_positions` is the only on-chain write path the Python side performs directly (everything else is via the sidecar). Flow:

1. Pull `redeemable=True` positions from `data-api.polymarket.com/positions`.
2. For each position, pick `OUTCOME_FACTORY_NEG` if `pos["negativeRisk"]` else `OUTCOME_FACTORY_STD`.
3. Encode `factory.redeemPositions(0x0, 0x0, conditionId, [1 << outcomeIndex])` calldata.
4. Wrap in a Gnosis Safe `execTransaction`, sign with `POLYMARKET_PRIVATE_KEY` (EIP-712), submit from EOA.
5. `eth_estimateGas` is the pre-flight: it reverts when there is nothing to redeem (already redeemed, market unresolved, zero wrapped balance), in which case the position is added to `_redeem_done_cache` and skipped without burning gas.
6. On successful receipt, the same cache marks the entry to prevent retry within the process lifetime (cache resets on container restart).

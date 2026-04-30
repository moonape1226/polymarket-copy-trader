# Book Logger Implementation Status

Observed: 2026-04-30.

This file describes what is currently present in the repository. The target design is `docs/book_logger_spec.md` v1.17.

| Area | Current repo state | Status vs v1.17 target |
|------|--------------------|---------------------|
| Standalone service | `docker-compose.yml` defines `book-logger`; code lives under `book_logger/` with `main.py`, `chain_subscriber.py`, `events_logger.py`, `token_meta.py`, `Dockerfile`, and `requirements.txt` | Matches standalone ownership model. The service is intentionally separate from `src/chain_feed.py`; do not add logger queues to the bot hot path |
| Phase 1 BS events | `book_logger/chain_subscriber.py` subscribes to V2 `OrderFilled` for tracked wallets as maker/taker, dedups by `(tx_hash, logIndex)`, and backfills on reconnect; `book_logger/events_logger.py` enriches in a worker thread and writes JSONL rows with `event_id`, decimal `log_index`, `order_hash`, `wallet_side`, metadata, block timestamp, and `tx_companions` | Output path `/data/book_logger/bs_events/` matches standalone layout. No observed `data/book_logger/` output in the workspace yet |
| Production chain feed | `src/chain_feed.py` still emits minimal copy events: `wallet`, `asset`, lowercase `side`, `size`, `price`, `tx_hash`, `block`, `received_ts` | Intentionally unchanged. This is bot-only copy-trader state, not a book-logger gap |
| Snapshot logger | No `book_snapshot.py` implementation found | Phase 2 WatchSet, REST `/book` snapshots, +1/+3/+10/+30/+60s event triggers, 10s ticks, and `data/book_logger/book_snapshots/` are not implemented |
| Weather universe | No `weather_universe.py` implementation found | Phase 2W lightweight all-weather market inventory under `data/book_logger/weather_universe/` is not implemented |
| Trade tape / own fills | No `trade_tape_poller.py` implementation found | Phase 3 token-scoped `eth_getLogs`, ring buffer replay, `data/book_logger/trade_tape/`, and `own_fills_queue` are not implemented |
| Copy decisions | `src/trading.py` still writes `data/copy_decisions.csv` and `data/vwap_skipped.csv` via `_log_copy_decision` / `_log_vwap_skipped` | Phase 4 append-only JSONL lifecycle rows under `data/book_logger/copy_decisions/`, `ACTION_MAP`, `pmxt_order_id` ledger, `own_order_hash`, `own_wallet_side`, `match_status`, and own-fill aggregation are not implemented |
| WS raw dump | No `book_ws_dumper.py` implementation found | Phase 5 CLOB WS raw dump to `data/book_logger/book_ws_raw/` is not implemented |
| Backtest tooling | No `tools/book_backtest.py` implementation found | Phase 6 REST snapshot backtest and grid search are not implemented |
| Runtime observation | `docker compose ps book-logger` showed no running `book-logger` container during this review | Start the service and collect 24h Phase 1 data before marking Phase 1 done |

# Book Logger Code Review

Review date: 2026-04-30.

Scope: `book_logger/*.py`, `book_logger/Dockerfile`, `book_logger/requirements.txt`, and `docker-compose.yml` service wiring.

## Findings

| Severity | Location | Finding | Impact | Recommendation |
|----------|----------|---------|--------|----------------|
| High | `book_logger/weather_universe.py:126-133`, `book_logger/weather_universe.py:273-291` | Closed/resolved grace logic is effectively unreachable because gamma query uses `active=true, closed=false`; closed markets stop appearing before the 1h grace/final resolution row can be written. | `weather_universe` will miss final resolution transitions and may under-report resolved outcomes. | Add a separate lookup for recently seen market IDs after they leave active results, or maintain a seen-market registry and poll their status for 1h after disappearance/closure. |
| High | `book_logger/weather_universe.py:241-248`, `book_logger/watch_set.py:66-82`, `book_logger/watch_set.py:97-104` | Weather promotions are added as `state="active"` and can evict oldest active WatchSet entries when cap is reached. There is no protection for BS-touched tokens. | Weather sample tokens can displace primary BS research tokens, causing missing snapshots for the core dataset. | Give BS-touched tokens eviction priority over weather-promoted tokens, or maintain a separate weather sample quota that cannot evict BS tokens. |
| High | `book_logger/book_snapshot.py:163`, `book_logger/watch_set.py:107-126` | BS tokens are added to WatchSet, but no `/positions` reconciliation marks exited positions as grace or expires grace entries. `mark_grace` and `expire_grace` have no caller. | WatchSet accumulates old active tokens and eventually evicts active entries incorrectly. | Implement periodic BS positions reconciliation: active while BS holds, mark grace after BS exits, expire grace after 24h. |
| Medium | `book_logger/chain_subscriber.py:182`, `book_logger/chain_subscriber.py:250-251` | Backfill starts from `_last_block + 1`, while live handling advances `_last_block` to any observed log's block. Disconnect mid-block can skip later logs from that block. | Rare but real data loss for same-block fills during reconnect. | Backfill with overlap, e.g. `max(0, _last_block - 5)`, and rely on `(tx_hash, log_index)` dedup. |
| Medium | `book_logger/token_meta.py:70-82` | Negative metadata misses are cached forever in-process. Gamma can lag on newly created/just-traded tokens. | Rows for a token can keep null metadata for the whole process lifetime after an early miss. | Replace permanent `_misses` with TTL misses, e.g. retry after 5-15 minutes. |
| Medium | `book_logger/weather_universe.py:224-238` | Promotion logic requires both `liquidity >= min` and within expiry. Spec allows top-N active weather by liquidity/volume as an independent promotion path. | Full-book weather sample may be narrower than intended and miss liquid non-near-expiry markets. | Implement two candidate paths: top-N by `liquidity_num desc, volume_24h_num desc, market asc`, plus near-expiry liquidity-qualified markets, then apply cap. |

## Verification

`python3 -m py_compile book_logger/*.py` passed.

No code changes were made during this review.

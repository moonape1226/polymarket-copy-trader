# Book-Snapshot Logger — Implementation Spec

Status: v1.17 — weather-universe lightweight collection added; ready to implement pending §11 sign-off.
Author / approver: BS-vs-ours debugging session 2026-04-29.

---

## 1. Goal

Collect 1–2 weeks of real Polymarket order-book data so we can decide, with evidence, whether to widen the VWAP gate that's currently blocking 100% of BS BUY copies.

The single research question this data must answer:

> For each historical BS BUY event, if our gate had been at tolerance level X, what would our realistic fill price have been, and what would our PnL have been once BS exited?

Everything else is secondary. The schema, triggers, and storage choices below all serve that question.

## 2. Non-goals

- Real-time strategy adjustment. This is offline data collection for backtest only.
- Universal LOB recording for all Polymarket markets. Full-book snapshots remain limited to BS-touched tokens plus explicitly promoted samples; lightweight weather-universe metadata is in scope.
- Polymarket's own internal CLOB metrics (fill rates, maker/taker stats system-wide).
- Replacing the bot's existing trade signal path. The logger runs alongside, never in front of.

## 3. Locked decisions (recap)

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Record full ask/bid levels (not top-N) | BS sometimes trades 1000+ shares; need depth to compute VWAP at large sizes |
| 2 | REST snapshot triggers at +1s, +3s, +10s, +30s, +60s after each BS chain event | Brackets typical bot decision latency (~3s) and post-event evolution |
| 3 | Continuous tick at 10s for tokens BS holds | Captures idle drift and BS exit-side book state |
| 4 | Replace `data/vwap_skipped.csv` with `data/book_logger/copy_decisions/` (logs both pass and skip) | Old CSV only logged skips, ~11 rows in 2 days, useless |
| 5 | Add trade tape (all fills on BS-touched tokens, not just BS's) | Critical for "did followers race in?" analysis |
| 6 | Path B: REST primary + WS raw dump as safety net (no in-memory book engine) | Avoids book-state-machine bug risk; v2 can build engine later if needed |
| 7 | String prices/sizes in `asks`/`bids` (Polymarket REST native) + `_num` mirrors for analysis | Stays compatible with any tool that already speaks Polymarket's REST format |
| 8 | Standalone `book-logger` service owns its own chain subscriber; `src/chain_feed.py` remains bot-only | Keeps research logging isolated from copy execution and avoids adding logger queues to the hot path |
| 9 | Collect a lightweight universe of all active weather markets; do not collect 10s full-book snapshots for every weather token by default | Gives calibration/baseline data without exploding REST `/book` calls or disk usage |

## 4. Data schemas

All book-logger files live under `data/book_logger/`. JSONL files are daily-rotated. Encoding UTF-8. Filename: `YYYY-MM-DD.jsonl`. Files older than 21 days gzipped to `.jsonl.gz` by daily cron (separate task — not part of v1).

### 4.1 `data/book_logger/book_snapshots/YYYY-MM-DD.jsonl`

The primary research dataset. One snapshot = one row.

```jsonc
{
  // Our metadata
  "ts": "2026-04-29T03:53:48.231Z",        // ISO 8601 UTC, millisecond precision (= snapshot_ts)
  "ts_ms": 1745898828231,                  // alias of snapshot_ts_ms (kept for back-compat)
  "trigger": "bs_event",                   // "bs_event" | "tick"
  "event_id": "0xc626ba25...:12",          // BS event composite id "{tx_hash}:{log_index}"; null if trigger=tick
  "offset_ms_from_received": 1000,         // schedule offset relative to event_received_ts; 0 if trigger=tick

  // Four timestamps make latency analysis unambiguous
  "event_block_ts_ms":    null,            // chain block timestamp; nullable in hot-path snapshots, backfilled by joining bs_events
  "event_received_ts_ms": 1745898828234,   // local clock when book_logger/chain_subscriber.py parsed the WS log (null if tick)
  "snapshot_ts_ms":       1745898828231,   // local clock right before REST /book sent
  "fetch_latency_ms": 142,                 // REST round-trip
  "fetch_method": "REST",                  // future-proof field

  "bs_order_side": "BUY",                  // raw V2 enum (maker order side); null if trigger=tick
  "bs_wallet_side": "BUY",                 // BS's actual direction; null if trigger=tick
  "bs_size": 489.47,
  "bs_price": 0.3500,
  "bs_role": "MAKER",                      // MAKER | TAKER, decoded from OrderFilled

  // Polymarket-native /book response inlined verbatim
  "market": "0xedc2dca850e6...",           // condition_id
  "asset_id": "76203583355404...",
  "outcome": "Yes",                        // "Yes" | "No"
  "neg_risk": false,
  "title": "Will the highest temperature in Seoul be 17°C on April 29?",
  "timestamp": "1745898828231",            // Polymarket string ms
  "hash": "0x9f3a...",                     // book hash for staleness check
  "asks": [
    {"price": "0.4100", "size": "1.69"},
    {"price": "0.4200", "size": "8.00"}
  ],
  "bids": [
    {"price": "0.3500", "size": "489.47"},
    {"price": "0.3400", "size": "250.00"}
  ],

  // Derived numerics for fast aggregation in pandas
  "best_ask_num": 0.4100,
  "best_bid_num": 0.3500,
  "mid_num": 0.3800,
  "spread_pct_num": 0.1579,
  "ask_depth_usd_num": 12.34,
  "bid_depth_usd_num": 28.91,
  "ask_levels": 5,
  "bid_levels": 8
}
```

Snapshot trigger rows are scheduled directly from `book_logger/chain_subscriber.py` using the decoded event payload, not from `events_logger.py` enrichment. In standalone mode, `src/chain_feed.py` is not involved. To keep +1s snapshots reliable, snapshot writes must not perform block-timestamp RPCs on the hot path: `event_block_ts_ms` may be `null` and should be filled analytically by joining `event_id` to `bs_events`. Metadata fields (`market`, `outcome`, `neg_risk`, `title`) are also nullable if `META.get(asset_id)` is unavailable at snapshot time; do not skip the snapshot because metadata is missing.

Volume estimate: see §7.1 (depends on WatchSet size; calibrated after Phase 2.5).

### 4.2 `data/book_logger/bs_events/YYYY-MM-DD.jsonl`

One row per BS chain OrderFilled event. Primary key: `event_id = "{lowercase_tx_hash}:{log_index_decimal_int}"`. Multiple BS events can share a tx (e.g. same-tx TAKER + MAKER, or same-tx YES+NO swaps).

**Encoding rules** (apply consistently across `bs_events`, `trade_tape`, `book_snapshots`, `copy_decisions`):
- `tx_hash` — lowercase 0x-prefixed hex string (`0xc626ba25...`)
- `log_index` — Python `int` (decimal), NOT hex string. RPC returns `logIndex` as `"0xc"`; convert to `12` immediately on decode.
- `event_id` — `f"{tx_hash}:{log_index}"` with the above. Example: `"0xc626ba25...:12"`.

Mismatched encoding (e.g. one writer using `:0xc`, another `:12`) silently breaks all cross-table joins. Phase 1 unit test must include a round-trip assertion: parse `event_id` back into `(tx_hash, log_index)` and verify both halves match the source RPC log after normalization.

```jsonc
{
  "event_id": "0xc626ba25...:12",          // primary key (composite tx_hash:log_index)
  "order_hash": "0xa4984cc6...",           // V2 OrderFilled topic[1] — EIP-712 hash of the maker order
  "ts": "2026-04-29T03:53:46.000Z",        // = block_ts
  "ts_ms": 1745898826000,                  // alias of block_ts_ms
  "block_ts_ms": 1745898826000,            // chain block timestamp
  "received_ts_ms": 1745898828234,         // local clock when book_logger/chain_subscriber.py parsed the WS log
  "tx_hash": "0xc626ba25...",             // lowercase hex string
  "block": 86157391,                       // int
  "log_index": 12,                         // int (decimal); raw RPC hex normalized to int on ingest
  "wallet": "0x331bf91c132af9d921e1908ca0979363fc47193f",
  "asset_id": "76203...",

  // Metadata (resolved from §5.7 Token metadata cache)
  "market": "0xedc2dca850e6...",           // condition_id
  "title": "Will the highest temperature in Seoul be 17°C on April 29?",
  "outcome": "Yes",                        // "Yes" | "No"
  "neg_risk": false,

  "order_side": "BUY",                     // V2 OrderFilled.side enum (the order's direction)
  "wallet_side": "BUY",                    // BS's actual direction (BUY / SELL); = order_side iff role=MAKER
  "size": 489.47,
  "price": 0.3500,
  "role": "MAKER",                         // BS's role: MAKER | TAKER
  "fee_amount_raw": "0",                   // V2 OrderFilled.fee, raw uint256 string (pUSD raw units)
  "fee_amount_num": 0.0,                   // pUSD numeric mirror
  "tx_companions": [                       // other fills in same tx (split/merge / hedge detection)
    {"event_id": "0xc626...:9",  "order_hash": "0x8c7e...", "asset_id": "...", "order_side": "BUY", "size": 30.0, "price": 0.38, "role": "no-BS"},
    {"event_id": "0xc626...:10", "order_hash": "0x4b1f...", "asset_id": "...", "order_side": "BUY", "size": 20.0, "price": 0.36, "role": "no-BS"}
  ]
}
```

Volume estimate: ~50 events/day × ~1.5 KB = 75 KB/day.

**Companion-fill source.** `tx_companions` cannot be populated from the wallet-scoped subscription alone — it only catches logs where BS is maker or taker. The receipt fetch happens in the **`book_logger/events_logger.py` worker thread** (off the standalone subscriber's WS handler; `chain_subscriber.py` already enqueued the raw log and returned). The worker synchronously calls `eth_getTransactionReceipt(tx_hash)` from its own thread and decodes every `OrderFilled` log in the same tx. Cost: one RPC per BS event (~200 ms × ~50 events/day = ~10 s/day total — borne by the worker, never by the subscriber). If the receipt fetch fails, write `tx_companions: null` and let the backtest `trade_tape_poller` join later by `tx_hash`.

### 4.3 `data/book_logger/trade_tape/YYYY-MM-DD.jsonl`

Every OrderFilled (any wallet) on tokens BS has touched. Aligned to Polymarket WS `last_trade_price` shape, augmented with chain provenance.

**Source path is separate from both `src/chain_feed.py` and `book_logger/chain_subscriber.py`.** Wallet-scoped subscriptions only see logs where the watched wallet is maker or taker; they can't see fills where neither side is BS. Trade tape uses a **token-scoped poller**: every block it issues `eth_getLogs` on V2 standard + neg-risk exchanges with `topics: [ORDER_FILLED]` (no wallet filter), then client-side filters to WatchSet token IDs (V2 `tokenId` is non-indexed so server-side filtering is impossible).

```jsonc
{
  "event_type": "last_trade_price",        // for tooling compatibility
  "event_id": "0xc626ba25...:12",          // composite id; matches bs_events.event_id when this fill is BS's
  "order_hash": "0xa4984cc6...",           // V2 OrderFilled topic[1] — maker order hash; own-order hash only if is_own_maker=true
  "ts": "2026-04-29T03:53:46.000Z",
  "ts_ms": 1745898826000,                  // = block_ts_ms
  "block_ts_ms": 1745898826000,
  "asset_id": "76203...",
  "market": "0xedc2...",                   // condition_id
  "price": "0.3500",                       // string per Polymarket convention
  "size": "489.47",
  "order_side": "BUY",                     // V2 OrderFilled.side enum (the maker order's direction)

  // Chain provenance
  "tx_hash": "0xc626ba25...",              // lowercase hex
  "block": 86157391,                       // int
  "log_index": 12,                         // int (decimal); see encoding rules in §4.2
  "maker": "0x331bf9...",
  "taker": "0x1f6679...",
  "is_bs_maker": true,
  "is_bs_taker": false,
  "is_own_maker": false,                   // maker == our_proxy
  "is_own_taker": false,                   // taker == our_proxy
  "own_wallet_side": null,                 // BUY | SELL when is_own_maker or is_own_taker, else null
  "fee_amount_raw": "0",                   // V2 OrderFilled.fee raw uint256 (string)
  "fee_amount_num": 0.0,                   // pUSD numeric mirror

  // Numeric mirrors
  "price_num": 0.3500,
  "size_num": 489.47
}
```

Scope: tokens are added to the watch set when BS first touches them (chain_feed event), kept for as long as `bs_position_size > 0` according to data-api `/positions`, plus 24h grace after BS exits to capture late post-exit activity.

Volume estimate: ~80 KB/day during BS-active hours, sometimes spiky.

### 4.4 `data/book_logger/copy_decisions/YYYY-MM-DD.jsonl`

Replaces `data/vwap_skipped.csv`. **Append-only lifecycle log**: each row is one stage in handling a BS event. Many rows can share the same `bs_event_id`. Backtest tool joins related rows.

Schema fields:
- `bs_event_id` — composite `{tx_hash}:{log_index}` of the BS event we're reacting to. **Nullable** — only the chain-fast path knows it; reconcile / activity / WS paths set it to `null`.
- `source_event_key` — required, always set. Format depends on signal source:
  - chain_fast: `chain:0xc626...:12` (= bs_event_id)
  - reconcile_slow: `reconcile:{asset_id}:{ts_ms}`
  - ws_fast_lane / ws_dual_signal: `ws:{asset_id}:{ts_ms}`
  - activity: `activity:{asset_id}:{ts_ms}`
  - startup_lookback: `startup:{asset_id}:{ts_ms}`
- `event_type` — lifecycle stage (enum below)
- `signal_source` — raw existing label (preserved verbatim from production code, not normalized)
- `decision_path` — normalized category: `chain` | `ws` | `reconcile` | `activity` | `startup`
- `skip_reason` — only when `event_type == "skipped"` (enum below)
- `fill_source` — only on `event_type == "filled"`; documents which authority confirmed (enum below)
- `order_hash` — V2 `OrderFilled.topic[1]`, the EIP-712 hash of the **maker's** order on this fill row. This is raw chain provenance, not a universal own-order id. It identifies our own submitted order only when `is_own_maker == true`.
- `own_order_hash` — our submitted order's EIP-712 hash if verified known, else `null`. If `PMXT_ID_IS_ORDER_HASH == true`, populate from `pmxt_order_id` on `submitted`; otherwise leave null until a maker-side chain fill proves it (`is_own_maker == true and order_hash` matches that submitted order). For taker fills, chain `order_hash` is the counterparty maker hash and must not be copied into `own_order_hash`.
- `pmxt_order_id` — pmxt's own id from `createOrder` response. Required on `submitted` / `open` / `cancelled`; present on `filled` only after matching a chain or non-chain fill to a tracked submitted order. It is the primary lifecycle aggregation key, but raw chain own-fill rows may initially have it as `null` until matched.
- `is_own_maker`, `is_own_taker` — booleans on chain fill rows; mutually exclusive. `is_own_maker == true` means chain `order_hash` is also our own maker order hash; `is_own_taker == true` means chain `order_hash` is the counterparty maker hash.
- `own_wallet_side` — `BUY` / `SELL` on own-fill rows, computed from `order_side` plus our maker/taker role; `null` on non-own rows. Matching and aggregation use this field, not maker-order `order_side`.
- `match_status` — on `filled` rows from `chain_orderfilled`: `matched_exact`, `matched_fuzzy`, `ambiguous`, or `unmatched`. Rows with `ambiguous` / `unmatched` keep raw provenance but are excluded from confident per-order aggregation.
- `PMXT_ID_IS_ORDER_HASH` / `PMXT_ID_HASH_STATUS` — Phase 1 constants. `PMXT_ID_IS_ORDER_HASH` is strictly boolean. `PMXT_ID_HASH_STATUS` is one of `"verified" | "plausible" | "false" | "unknown"`. A hash-shaped pmxt id may set status to `"plausible"`, but the boolean remains `False` until maker-side chain evidence proves equality.
- `fill_event_id` — present on `filled` rows when `fill_source == "chain_orderfilled"`. Format `"{tx_hash}:{log_index}"` matching `event_id` rules in §4.2. **Top-level field** (not nested in `fill`) so dedup can read it directly.
- per-stage payload depending on `event_type`

```jsonc
// Example 1: a "skipped" row (no order placed)
{
  "ts": "2026-04-29T03:53:48.231Z",
  "ts_ms": 1745898828231,
  "bs_event_id": "0xc626ba25...:12",
  "source_event_key": "chain:0xc626ba25...:12",
  "asset_id": "...",
  "event_type": "skipped",
  "signal_source": "chain_fast",
  "decision_path": "chain",
  "skip_reason": "vwap",

  "config_snapshot": {
    "vwap_tolerance_by_bucket": {"lt_0_05": 0.40, "0_05_to_0_15": 0.25, "0_15_to_0_50": 0.15, "gte_0_50": 0.05},
    "buy_limit_slip_pct": 0.15,
    "buy_limit_slip_pct_low_prob": 0.40,
    "low_prob_price_threshold": 0.30,
    "low_prob_max_portfolio_pct": 0.20,
    "max_position_usd": 1500.0,
    "min_trade_usd": 5.0
  },
  "computed": {
    "our_size": 489.47,
    "is_low_prob": false,
    "vwap_estimate": 0.6740,
    "vwap_tolerance_used": 0.15,
    "vwap_max_allowed": 0.4025,
    "limit_price": 0.4025,
    "depth_filled_pct": 100.0,
    "depth_filled_shares": 489.47
  }
}

// Example 2: a successful pipeline emits multiple rows (all sharing bs_event_id / source_event_key).
// Note: decision_path is the normalized category; signal_source preserves the production label verbatim.
// Note: pmxt_order_id threads through submitted → open → filled as the lifecycle join key.
{ "event_type": "detected",  "bs_event_id": "...", "signal_source": "chain_fast", "decision_path": "chain", ... }
{ "event_type": "evaluated", "bs_event_id": "...", "signal_source": "chain_fast", "decision_path": "chain", "computed": { ... }, ... }   // pass
{ "event_type": "submitted", "bs_event_id": "...", "signal_source": "chain_fast", "decision_path": "chain", "pmxt_order_id": "abc-123", "own_order_hash": null, "order": {"limit_px":0.4025, "size":489.47, "own_wallet_side":"BUY"}, ... }
{ "event_type": "filled",    "bs_event_id": "...", "signal_source": "chain_fast", "decision_path": "chain", "pmxt_order_id": "abc-123", "own_order_hash": null, "order_hash": "0xmaker...", "own_wallet_side": "BUY", "match_status": "matched_fuzzy", "fill_source": "chain_orderfilled", "fill_event_id": "0xabcd...:7", "tx_hash": "0xabcd...", "log_index": 7, "fill": {"filled_sh":489.47, "filled_avg_px":0.4012, "is_partial": false}, ... }

// Example 3: a non-chain path (reconcile) — bs_event_id is null, source_event_key carries the join key
{ "event_type": "detected", "bs_event_id": null, "source_event_key": "reconcile:7620...:1745898828231", "signal_source": "reconcile_slow", "decision_path": "reconcile", ... }
{ "event_type": "skipped",  "bs_event_id": null, "source_event_key": "reconcile:7620...:1745898828231", "signal_source": "reconcile_slow", "decision_path": "reconcile", "skip_reason": "reconcile_stale_no_ask", ... }

// Example 4: an order that submitted but stayed open through TTL expiry
{ "event_type": "submitted", "bs_event_id": "...", "pmxt_order_id": "abc-123", "own_order_hash": null, "order": { ... }, ... }
{ "event_type": "open",      "bs_event_id": "...", "pmxt_order_id": "abc-123", "own_order_hash": null, "order": {"remaining_sh": 489.47}, "ts_ms": ..., ... }   // periodic, every 5 min
{ "event_type": "cancelled", "bs_event_id": "...", "pmxt_order_id": "abc-123", "own_order_hash": null, "order": {"filled_sh": 0, "remaining_sh": 489.47}, "reason": "ttl_expired", ... }
```

`event_type` enum (lifecycle):
- `detected` — signal handed to copy pipeline (chain_feed, reconcile poll, WS, activity, or startup lookback)
- `evaluated` — gate logic ran (snapshot of `computed` + `config_snapshot`); whether pass or skip is implied by adjacent row
- `skipped` — no order placed; `skip_reason` set
- `submitted` — order sent to pmxt sidecar; **may be terminal** (limit order sits open until TTL)
- `open` — periodic lifecycle ping (every 5 min) confirming a `submitted` order is still alive on book; carries `remaining_sh`
- `filled` — order filled (full or partial); `fill_source` set; `fill.is_partial` distinguishes
- `failed` — order rejected by sidecar, signing error, or unhandled exception during submission
- `cancelled` — order cancelled (TTL expiry, manual, or cleanup); `reason` field set

`skip_reason` enum (initial set; **mapping must be exhaustive in Phase 4**):
- `vwap` — VWAP gate over tolerance
- `vwap_empty_asks` — no asks at all
- `max_position` — would exceed max_position_usd
- `blocked_title` — title matches blocked_title_keywords
- `low_prob_cap` — would exceed low_prob portfolio %
- `already_holding` — duplicate buy on existing position
- `min_trade` — under min_trade_usd
- `min_shares` — BS share size below min_target_shares
- `min_order` — would emit order under Polymarket $1 minimum
- `size_zero` — computed our_size <= 0
- `unknown_price` — bs_price missing and unknown_price filter rejected
- `max_buy_price` — BS price above max_buy_price
- `position_not_tracked` — sell signal but we don't hold
- `reconcile_stale_no_ask` — reconcile path, no current ask available
- `reconcile_stale_drift` — reconcile path, mid drifted past tolerance

Phase 4 must grep all production `_log_copy_decision` / `_log_vwap_skipped` callers and ensure every existing label has a mapping; if a label appears that doesn't fit, extend the enum rather than collapsing into `error`.

`fill_source` enum (priority order — when multiple sources confirm the same fill, take highest-priority):
1. `chain_orderfilled` — own-wallet `OrderFilled` event observed via **`trade_tape_poller`'s own-fill side-output** (NOT `chain_feed` — that subscription is wallet-scoped to BS only; adding our proxy would pollute the copy queue with self-fill loops). The poller already enumerates all OrderFilled on V2 exchanges; it side-outputs any fill where `maker == proxy or taker == proxy`. **Authoritative**, settled.
2. `sidecar_immediate` — pmxt `createOrder` response includes immediate fill; fast but may be incomplete
3. `clob_polling` — `/trades` or `/openorders` poll picked up partial / late fill
4. `activity_recon` — `/activity` poll backstop (slowest)

Each source emits its own `filled` row with `fill_source` set.

**Chain own-fill matching algorithm** (Phase 4, `copy_decision_log.py` consuming `own_fills_queue`):

1. Maintain an in-process submitted-order ledger keyed by `pmxt_order_id`. Each entry stores `asset_id`, `own_wallet_side`, `submitted_ts_ms`, `limit_px`, original `size`, current `remaining_sh`, `bs_event_id`, `source_event_key`, optional `own_order_hash`, and terminal status if cancelled/filled.
2. For every chain own-fill, compute `own_wallet_side` from maker-order `order_side` plus our role: maker keeps `order_side`; taker gets the opposite side. Do not use raw `order_side` for matching when `is_own_taker == true`.
3. Exact path: if `is_own_maker == true` and chain `order_hash` equals a ledger entry's non-null `own_order_hash`, set that entry's `pmxt_order_id`, `own_order_hash`, `bs_event_id`, `source_event_key`, and `match_status: "matched_exact"` on the `filled` row.
4. Fuzzy path: otherwise search active ledger entries where `asset_id` and `own_wallet_side` match, `fill_received_ts_ms >= submitted_ts_ms - 5000`, the entry was not terminal before the fill, the fill price is compatible with the submitted limit (`BUY`: `fill_px <= limit_px`; `SELL`: `fill_px >= limit_px`), and `remaining_sh >= filled_sh - 1e-6`. If exactly one candidate remains, attach its `pmxt_order_id` and write `match_status: "matched_fuzzy"`.
5. Ambiguity path: if multiple candidates remain, write the raw `filled` row with `pmxt_order_id: null`, `match_status: "ambiguous"`, and `candidate_pmxt_order_ids: [...]`. Preserve the data, but exclude it from confident aggregation.
6. Miss path: if no candidate remains, write the raw `filled` row with `pmxt_order_id: null`, `match_status: "unmatched"`. Preserve the data for manual inspection and activity reconciliation.
7. After any `matched_exact` / `matched_fuzzy` row, decrement that ledger entry's `remaining_sh` by `filled_sh` and update `fill.is_partial` from the running filled total versus submitted size.

**Two-layer dedup** (deduping `(order_hash, fill_source)` as a single key would collapse legitimate partial fills — one order can emit multiple OrderFilled logs over time and across txs; each is a distinct fill event):

1. **Per-fill identity** (which fill event is this row reporting?):
   - For `fill_source == "chain_orderfilled"`: primary key `(tx_hash, log_index)` — top-level fields on the row (read `fill_event_id` for convenience). Two rows with the same pair are reporting the same on-chain fill event — dedup to one, prefer the row written first. (Note: not keyed on `order_hash` because per-fill identity is uniquely determined by chain coordinates regardless of which side of the trade we are on.)
   - For `fill_source == "sidecar_immediate"`: **provisional aggregate** observation. pmxt's `createOrder` response can return a `fills: [...]` array that aggregates multiple book hits into one entry (e.g. "100 sh @ avg $0.40") while chain emits one `OrderFilled` per matched maker order (e.g. "20 sh @ $0.39 + 30 sh @ $0.40 + 50 sh @ $0.41"). DO NOT 1:1 dedup against chain rows. Instead:
     - Mark the row `provisional: true`, `fill_source: "sidecar_immediate"`, key `(pmxt_order_id, fills_index_in_response)`
     - On chain fills arriving for the same `pmxt_order_id` within 60s window, the chain rows REPLACE the sidecar aggregate in the dedup'd output: drop the sidecar row, keep all chain rows. If no chain rows arrive (rare — sidecar lied about fill?), keep the sidecar row.
   - For `fill_source ∈ {"clob_polling", "activity_recon"}`: primary key `(coalesce(pmxt_order_id, own_order_hash, order_hash), fill_source, source_seq)`. `pmxt_order_id` wins whenever known; `order_hash` is last because it may be the counterparty maker hash on taker fills.
   - **`source_seq` definitions** (per source):
      - `sidecar_immediate`: index into pmxt response's `fills` array (0, 1, 2, ...). `fills` is the actual pmxt field name; if pmxt rename or schema diverges, the `book_logger/copy_decision_log.py` normalization layer maps it.
     - `clob_polling`: `f"{poll_ts_ms}:{idx_in_response}"`
     - `activity_recon`: priority chain — first available of:
       1. `transactionHash` if present (strong key)
       2. `txHash` if present (strong key)
       3. `hash` if present (strong key)
       4. Fallback: `f"weak:{asset_id}:{own_wallet_side}:{timestamp}:{size}"` (weak key)
       Each `activity_recon` row also carries `key_strength: "strong" | "weak"`. Rows with `key_strength == "weak"` raise a `data_uncertain` flag in the backtest aggregation report so the user knows which conclusions rely on fuzzy joins.
   - **Cross-source dedup**: when a later `chain_orderfilled` row matches a non-chain row by `(pmxt_order_id, own_wallet_side, filled_sh, filled_avg_px ± 0.001, ts ± 5s)`, the chain row wins (highest priority). Backtest may backfill `own_order_hash` only when the chain row is `is_own_maker == true`; it must not treat taker-side chain `order_hash` as our own hash.

2. **Per-order aggregation** (how much of the original order has filled?):
   - Group all dedup'd fill rows with non-null **`pmxt_order_id`** by that key. Rows with `match_status ∈ {"ambiguous", "unmatched"}` are kept in raw outputs but excluded from confident per-order totals.
   - When `is_own_maker == true` on a chain fill, `order_hash` is also recorded as `own_order_hash` for that specific submitted order if previously unknown. It remains secondary analysis metadata, not the group key.
   - Sum `filled_sh`; running total reconciles against `submitted.size` to compute partial vs full
   - The submitted-order row joins to its fills via `pmxt_order_id`

**Why `pmxt_order_id` is the primary join key, not `order_hash`**: V2 `OrderFilled.topic[1]` is the **maker order's** EIP-712 hash. When our wallet is the taker (we crossed the spread), `topics[1]` is the **counterparty maker's** hash, not ours; our own order hash is never emitted on a fill we initiated as taker. So `order_hash` only works as an own-fill join key when `is_own_maker == true`. After the §4.4 matching algorithm attaches it, `pmxt_order_id` works for all our fills regardless of role and is therefore the lifecycle join key.

### 4.5 `data/book_logger/book_ws_raw/YYYY-MM-DD.jsonl`

WS messages dumped verbatim, no parsing. v1 backtest does not consume this; it's for v2 use.

```jsonc
{
  "_logged_at_ms": 1745898828231,          // our local receive time
  // ↓ Polymarket WS message verbatim
  "event_type": "book",
  "asset_id": "...",
  "market": "...",
  "buys": [...],
  "sells": [...],
  "hash": "...",
  "timestamp": "..."
}
```

Subscriptions: same watch set as trade tape (BS's currently-touched tokens + 24h grace).

Volume estimate: ~30 MB/day raw, ~8 MB gzipped.

### 4.6 `data/book_logger/weather_universe/YYYY-MM-DD.jsonl`

Lightweight inventory of all active weather markets. This is for baseline/calibration: how many weather markets existed, what their probabilities/liquidity/volume looked like, and how they resolved. It is **not** a full LOB dataset.

Collection cadence: every 15 minutes for active markets. After a market first appears with `closed=true` or `resolved=true`, keep it in the active poll set for 1 hour grace to catch final resolution fields, then remove it from polling. If `resolution_outcome` changes during that grace window, write the changed row immediately.

Weather classification: include markets with weather-related tags from gamma-api and title/category heuristics such as temperature, rainfall, snowfall, wind, hurricane, storm, air quality, and named city/date weather questions. Exclude generic politics/sports/crypto unless the market's primary event is weather.

```jsonc
{
  "ts": "2026-04-29T03:45:00.000Z",
  "ts_ms": 1745898300000,
  "market": "0xedc2dca850e6...",          // condition_id
  "event_id": "12345",                    // gamma event id if present
  "slug": "will-seoul-high-temp-be-17c-apr-29",
  "title": "Will the highest temperature in Seoul be 17°C on April 29?",
  "category": "Weather",
  "tags": ["Weather", "Temperature"],
  "end_date_iso": "2026-04-29T23:59:00Z",
  "active": true,
  "closed": false,
  "resolved": false,
  "resolution_outcome": null,

  "tokens": [
    {"asset_id": "76203...", "outcome": "Yes", "price_num": 0.35},
    {"asset_id": "99188...", "outcome": "No",  "price_num": 0.65}
  ],

  "best_bid_num": 0.34,                    // optional aggregate from gamma/CLOB if cheap
  "best_ask_num": 0.36,
  "volume_24h_num": 12345.67,
  "volume_total_num": 45678.90,
  "liquidity_num": 8901.23,
  "source": "gamma_markets"
}
```

Write policy: write one full lightweight row for every included weather market on every 15-minute poll. Do not suppress identical rows and do not add change-threshold logic in v1. At the expected ~2-5 MB/day volume, simple append-only rows are cheaper than making logger semantics branchy. Backtest tooling can dedup or downsample analytically if needed.

Do **not** call REST `/book` every 10s for every weather token. A weather token is promoted into the full `book_snapshots` WatchSet only if at least one condition is true:

- BS touches the token.
- It is selected by an explicit weather sample cap, e.g. top `N=20` active weather markets after sorting by `liquidity_num desc`, then `volume_24h_num desc`, then `market` ascending for deterministic ties.
- It is within a configured expiry window, e.g. `<24h`, and passes liquidity threshold.
- User explicitly enables full-weather-book collection after Phase 2.5 rate/disk calibration.

If more than `weather_full_book_sample_cap` non-BS weather markets satisfy promotion criteria, keep only the top markets using the same sort: `liquidity_num desc`, then `volume_24h_num desc`, then `market` ascending. BS-touched tokens do not count against this cap.

Volume estimate: lightweight rows are expected to be small. Typical: 30-60 active weather markets × 96 polls/day × ~400-600 bytes ≈ 2-5 MB/day raw, usually much less gzipped. Theoretical max for unusually broad weather coverage is ~50 MB/day. The limiting factor for full-book collection is REST call volume, not only disk.

## 5. Architecture

### 5.1 New modules

```
book_logger/chain_subscriber.py — standalone wallet-scoped V2 subscriber for BS OrderFilled events
book_logger/events_logger.py    — worker thread consuming subscriber queue; enriches BS chain events
book_logger/token_meta.py       — shared metadata cache (gamma-api lookup, persisted)
book_logger/book_snapshot.py    — REST poller; main research data source; owns WatchSet singleton
book_logger/trade_tape_poller.py — token-scoped eth_getLogs; trade tape + own-fill side-output
book_logger/book_ws_dumper.py   — WS raw dumper; v2 safety net
book_logger/copy_decision_log.py — JSONL writer for decision audit (replaces vwap_skipped.csv writer if enabled)
book_logger/weather_universe.py — lightweight all-weather market inventory + optional sample promotion
tools/book_backtest.py          — v1 backtest using REST snapshots
tests/test_action_map.py        — enforces ACTION_MAP completeness against production labels
```

### 5.2 Watch-set management

A single in-memory `WatchSet` shared between `book_snapshot.py`, `book_ws_dumper.py`, and the trade-tape poller. Defines which tokens to record.

Membership rules:
- Token added when `book_logger/chain_subscriber.py` sees BS OrderFilled on it
- Token added when periodic data-api `/positions?user=<BS>` poll (every 5 min) shows BS holds it
- Weather token added only when promoted by §4.6 full-book promotion criteria; ordinary weather-universe rows do not enter WatchSet
- Token kept while BS holds it (`size > 0` per /positions) — marked `state: "active"`
- Token kept 24 hours past last BS activity (catch late post-exit ticks) — marked `state: "grace"`
- Hard cap: 50 tokens.

**Eviction priority** (when at cap and a new token must be admitted):
1. Evict tokens with `state == "grace"` first, oldest `last_touch_ms` first.
2. If all 50 are `state == "active"` (rare — BS holds 50+ markets simultaneously), evict the oldest active. **This is a data-loss event**: write a row to `data/book_logger/watchset_evicted.log`, increment a `data_incomplete` counter, and post a Slack alert. Backtest tool sees `data_incomplete=true` flag in calibration metadata for the affected day and excludes that token's rows from confidence-bounded results.

**Persistence**: WatchSet state is dumped to `data/book_logger/watchset.json` on every change (atomic write — temp file + rename). On book-logger startup, the file is loaded and merged with the next /positions poll. Entries with `last_touch_ms` older than 24h are dropped during load. This preserves the 24h grace window across restarts; without persistence, recently-exited tokens drop off trade tape and ws_raw immediately on restart.

Implementation: `WatchSet` lives in `book_logger/book_snapshot.py`, exposed as a module-level singleton. Other book-logger modules import and consult it.

### 5.3 Integration Model

The book logger is a standalone Docker service. It does **not** patch or wrap `src/chain_feed.py`; the copy-trader's chain feed remains bot-only and keeps its existing queue/event shape.

```
                  ┌────────────────────────────┐
                  │ book_logger/chain_subscriber.py
                  │ wallet-scoped V2 WS logs   │
                  └──────────────┬─────────────┘
                                 │ internal non-blocking queues
                 ┌───────────────┴────────────────┐
                 ▼                                ▼
        events_logger.py                 book_snapshot.py
        slow enrichment                   +1/+3/+10/+30/+60s
        → bs_events/*.jsonl               + 10s tick snapshots
                 │                        → book_snapshots/*.jsonl
                 │
                 ▼
        data/book_logger/bs_events/*.jsonl

                  ┌────────────────────────────┐
                  │ trade_tape_poller.py       │
                  │ token-scoped eth_getLogs   │
                  └──────────────┬─────────────┘
                                 ▼
        data/book_logger/trade_tape/*.jsonl + own_fills_queue

                  ┌────────────────────────────┐
                  │ book_ws_dumper.py          │
                  │ Polymarket CLOB WS dump    │
                  └──────────────┬─────────────┘
                                 ▼
        data/book_logger/book_ws_raw/*.jsonl
```

Phase 4 copy-decision logging is the only part that may touch bot order-placement code. If enabled, it writes JSONL under `data/book_logger/copy_decisions/` and must not change copy execution behavior.

### 5.4 Threading

- `book_logger/chain_subscriber.py`: dedicated thread / asyncio event loop with its own wallet-scoped Polygon WS subscriptions. Emits raw logs to an internal bounded events queue; starting Phase 2, also emits decoded trigger payloads to `snapshot_trigger_queue`. It is independent of `src/chain_feed.py`.
- `book_logger/events_logger.py`: dedicated worker thread consuming the internal events queue. Performs all enrichment (block_ts fetch, receipt fetch, companions decode, metadata lookup) here — never on the subscriber's WS handler thread.
- `book_logger/book_snapshot.py`: dedicated thread with its own asyncio event loop. Consumes `snapshot_trigger_queue` and maintains a priority queue of `(due_at_ms, fetch_task)` tuples. Tick scheduler enqueues every 10s; event triggers enqueue 5 tasks at offsets +1/+3/+10/+30/+60s relative to `event_received_ts_ms`.
- `book_logger/book_ws_dumper.py`: dedicated thread, asyncio event loop, single WS connection with multi-token subscription. Reconnects with exponential backoff (5s → 60s).
- `book_logger/weather_universe.py`: dedicated periodic poller (default every 15 min) querying gamma-api for active weather markets and writing lightweight rows. It may promote a bounded subset of weather tokens into WatchSet using §4.6 criteria.
- `book_logger/trade_tape_poller.py`: dedicated thread; per-block (every ~2s on Polygon) `eth_getLogs` against V2 standard + neg-risk exchanges with topic = `OrderFilled`, no wallet filter. Client-side filters to WatchSet tokens. Tracks `last_processed_block` watermark. **Side-output**: any log where `maker == our_proxy or taker == our_proxy` is also emitted to `own_fills_queue` with `own_wallet_side` (consumed by `copy_decision_log.py` to write `filled` rows with `fill_source: "chain_orderfilled"`). This avoids polluting BS's copy-trader `chain_feed` subscription with our own wallet.
- `src/trading.py` or a thin bot-side adapter: synchronous JSONL appends to `data/book_logger/copy_decisions/` only if Phase 4 is enabled; order placement behavior stays unchanged.

All writers use a shared `JsonlWriter` helper with thread-safe append.

### 5.5 Failure modes & handling

| Failure | Detection | Handling |
|---------|-----------|----------|
| REST `/book` 5xx / timeout | response status / asyncio timeout | log warning, skip this snapshot, set `fetch_method: "REST_FAILED"` row with error field |
| WS disconnect | websockets exception | reconnect with backoff; log gap to `book_ws_raw` as `{"_event":"ws_disconnect", ...}` row |
| WatchSet full | size check on insert | evict per §5.2 priority: `state="grace"` first by oldest `last_touch_ms`; if all 50 are `state="active"`, evict the oldest active and write a row to `data/book_logger/watchset_evicted.log`, increment `data_incomplete` counter, post a Slack alert |
| Disk full | `OSError` on append | log to stderr; bot continues; failing logger does not crash bot |
| Book-logger restart | logger threads spawn fresh | startup sequence: (1) load `data/book_logger/watchset.json`, (2) drop entries with `last_touch_ms` older than 24h, (3) merge with first /positions poll, (4) resume snapshot scheduling and trade tape polling. `book_ws_raw` has a gap during reconnect; REST poller covers it. trade_tape_poller starts at `last_processed_block` from disk if persisted, else `head - 30`. |

### 5.6 What this DOES NOT change

- Existing `src/chain_feed.py` event flow → copy queue path: untouched. Standalone book-logger uses `book_logger/chain_subscriber.py`; do **not** add logger queues to the bot chain feed.
- Existing `trading.py` order-placement logic: untouched. Phase 4 may add decision logging, but must not change order submission/cancel/fill behavior.
- Existing `redeemer.py`, `notifier.py`, `positions.py`: untouched
- Existing bot config keys: nothing renamed. Standalone service can be disabled by stopping `book-logger`; optional `book_logger_enabled` may gate the service internally if implemented.

### 5.7 Token metadata cache

Raw V2 OrderFilled logs carry only `tokenId`. Every JSONL schema needs richer fields: `title`, `outcome` (Yes/No), `neg_risk` (bool), `market` (= conditionId). These come from a shared **token metadata cache**:

- **Source**: gamma-api `GET /markets?clob_token_ids=<tokenId>` returns one market object. Polymarket's actual field names (verified against live gamma-api response):
  ```python
  m = gamma_response[0]
  question      = m["question"]                          # → schema "title"
  outcomes_raw  = json.loads(m["outcomes"])              # ["Yes", "No"]
  token_ids     = json.loads(m["clobTokenIds"])          # parallel list of token id strings
  i             = token_ids.index(asset_id_str)          # find which outcome this token belongs to
  outcome       = outcomes_raw[i]                        # → schema "outcome"
  neg_risk      = bool(m.get("negRisk", False))          # → schema "neg_risk"
  market_id     = m["conditionId"]                       # → schema "market"
  ```
  Note: gamma-api uses `question` not `title`, and `clobTokenIds`/`outcomes` are JSON-encoded strings inside the response (not native lists).
- **Cache structure**: in-memory `dict[token_id_str → MetaRecord]`. Populated lazily on first need (any logger that mentions a token consults the cache; cache miss triggers fetch).
- **Persistence**: dumped to `data/book_logger/token_meta.json` (atomic write — temp + rename) on every new entry. Loaded on startup. No expiry — once a market is created, its metadata doesn't change.
- **Failure handling**: gamma-api 5xx / timeout / token not in any returned market → write the row with `title: null, outcome: null, neg_risk: null, market: null`. Backtest tool can backfill metadata later by re-querying gamma-api against rows with null fields. Do NOT skip writing the row — the rest of the data (book, prices, sizes) is still useful.
- **Implementation**: lives in `book_logger/token_meta.py`, exported as singleton `META`. All book-logger modules import it. ~70 lines.

## 6. Implementation phases

Current repository implementation status is tracked separately in `docs/book_logger_implementation_status.md`.

### Phase 1: BS events logger

- New files:
  - `book_logger/chain_subscriber.py` — standalone wallet-scoped V2 subscriber; owns its own WS connections and dedup
  - `book_logger/events_logger.py` — worker thread consuming subscriber queue, doing all enrichment off the WS handler path
  - `book_logger/token_meta.py` — shared metadata cache (§5.7)
- **Standalone mode: `src/chain_feed.py` is unchanged.** The book logger uses its own subscriber and must not add `logger_queue` or `snapshot_trigger_queue` to the copy-trader hot path.
- `book_logger/chain_subscriber.py` pushes raw logs to the internal events queue non-blockingly:
  ```python
  try:
      events_queue.put_nowait({
          "raw_log": raw_log_dict,
          "received_ts": time.time(),
          "source": provider_tag,
      })
  except queue.Full:
      EVENTS_DROPPED_COUNTER += 1
  ```
- Starting Phase 2, the same subscriber also pushes decoded event trigger payloads to `snapshot_trigger_queue` for immediate snapshot scheduling:
  ```python
  try:
      snapshot_trigger_queue.put_nowait({
          "event_id": event_id,
          "tx_hash": tx_hash,
          "log_index": log_index,
          "block": block_number,
          "asset_id": asset_id_decoded,
          "event_received_ts_ms": received_ts_ms,
          "event_block_ts_ms": None,                      # no block-ts RPC on subscriber hot path
          "bs_order_side": order_side,
          "bs_wallet_side": wallet_side,
          "bs_role": role,
          "bs_size": shares,
          "bs_price": price,
      })
  except queue.Full:
      SNAPSHOT_TRIGGER_DROPPED_COUNTER += 1
  ```
  Both queues bounded at `maxsize=10000`. The two queues are independent so that book_snapshot's +1s scheduling is never delayed by events_logger's enrichment latency. Both `Full` branches increment counters and never raise. `snapshot_trigger_queue` carries only fields already decoded by the standalone subscriber; it must not perform block timestamp RPCs or metadata lookups on the WS handler path.
  Drop counters are exposed via the §7.3 monitoring summary; any non-zero `*_drops_24h` triggers a Slack alert (probably means a downstream worker died).
  Raw log carries `topics`, `data`, `transactionHash`, `logIndex`, `blockNumber`, plus the local `received_ts`. Nothing else runs on the subscriber's WS handler thread.
- **Enrichment happens entirely in the `book_logger/events_logger.py` worker thread** (so RPC and gamma-api fetches never delay event ingestion):
  - `log_index` — `int(raw_log["logIndex"], 16)` — RPC returns hex string; normalize to decimal int immediately on ingest
  - `tx_hash` — `raw_log["transactionHash"].lower()` — lowercase normalization
  - `event_id` — `f"{tx_hash}:{log_index}"` after both above normalizations
  - `order_hash` — `topics[1]` (V2 OrderFilled emits the EIP-712 order hash here as bytes32); raw 0x-prefixed lowercase string
  - `order_side_enum` — raw V2 `Side` (0=BUY, 1=SELL); maps to schema `order_side`
  - `wallet_side` — `"BUY"` if (`role==MAKER` and `order_side==BUY`) or (`role==TAKER` and `order_side==SELL`) else `"SELL"`
  - `role` — `"MAKER"` if `topics[2][-40:] == BS_addr` else `"TAKER"`
  - `fee_amount_raw` — decode from `data[256:320]`; `fee_amount_num = int(raw)/1e6`
  - `block_ts_ms` — `eth_getBlockByNumber(block, false)`; per-block LRU cache (one fetch per block shared by all events in it)
  - Metadata (`title`, `outcome`, `neg_risk`, `market`) via `META.get(asset_id)` from §5.7 cache; null on miss
  - Companion fills via `eth_getTransactionReceipt(tx_hash)` (cached by tx_hash); decode every OrderFilled in the tx including each `order_hash` from `topics[1]` → `tx_companions`. On RPC failure, write `tx_companions: null`.
- **Sub-task before Phase 1 ships: empirically determine `pmxt_order_id` ↔ `own_order_hash` relationship.**
  1. Pick a low-priority asset with non-zero ask depth
  2. Place a **marketable** test order — limit price = `best_ask × 1.02` (small buffer to guarantee crossing the spread), size = `ceil(1.10 / limit_price)` shares (just over the $1 minimum). The order MUST cross the spread; a deep resting limit will not produce an `OrderFilled` and the experiment cannot conclude.
  3. **Important**: because we are the taker on this test order, the on-chain `OrderFilled` events emit `topics[1]` = the COUNTERPARTY maker's hash, not ours. So this test cannot directly compare `pmxt.id` to a single chain `topics[1]`. Instead the test verifies via the inverse:
     - Does pmxt response `id` look like a 32-byte hex hash (`^0x[0-9a-f]{64}$`)? If yes, it's plausible (but not proven) to be the EIP-712 hash. Set `PMXT_ID_HASH_STATUS = "plausible"` and keep `PMXT_ID_IS_ORDER_HASH = False` until maker-side proof exists.
     - To prove it, place a **second** test as a maker — a deep limit BUY at `best_bid × 0.95` so it rests on book, then have the user manually hit it from a different account or wait for any taker. Once the maker fill happens, our wallet is the maker on that chain row → `topics[1]` IS our order hash. Compare against pmxt's `id` from the test placement. This confirms or disproves equality.
     - If the second test isn't possible (no manual second account), conservatively set `PMXT_ID_IS_ORDER_HASH = False`, `PMXT_ID_HASH_STATUS = "unknown"` (or `"plausible"` if only hash-shape was observed), and rely on `pmxt_order_id` as the lifecycle join key after matching.
  4. Even when `PMXT_ID_IS_ORDER_HASH = True`, the **taker-side fills still cannot be joined by `order_hash`** because the chain emits the counterparty's hash on those rows. So `pmxt_order_id` remains the primary join key after matching; `order_hash` is secondary metadata only valid as our own hash when `is_own_maker == true`.
  5. Document the finding inline in `book_logger/copy_decision_log.py` as `PMXT_ID_IS_ORDER_HASH: bool = ...` and `PMXT_ID_HASH_STATUS: Literal["verified", "plausible", "false", "unknown"] = ...`, plus a comment recording the test tx hashes for audit.
  - This sub-task replaces the earlier "compute locally from signed order struct" — Python doesn't hold the signed struct (pmxt sidecar does the signing), so local recomputation is not viable.
- Unit test on a known V2 OrderFilled receipt (e.g. `0xc626ba25...` Seoul 17°C from 2026-04-29). Tests must include:
  - `event_id` round-trip: parse `f"{tx}:{idx}"` back, assert both halves match the source RPC log after lowercase + decimal-int normalization
  - `log_index` is `int` type, never `str`, never hex
- Output: `data/book_logger/bs_events/YYYY-MM-DD.jsonl`
- **Done when**:
  1. 24h of bot uptime produces non-empty `bs_events` file matching BS activity from `/activity` API
  2. All schema fields populated (`tx_companions` non-empty for at least one same-tx multi-fill case; metadata fields `title`/`outcome`/`neg_risk`/`market` populated for ≥95% of rows)
  3. `book-logger` WS receive→queue-put latency remains low under load, and copy-trader `Chain-flush` latency is unchanged because `src/chain_feed.py` is untouched
  4. `PMXT_ID_IS_ORDER_HASH` and `PMXT_ID_HASH_STATUS` constants determined and documented
  5. `events_drops_24h` metric reads 0 over the test window

### Phase 2: WatchSet + book_snapshot REST poller

- New file: `book_logger/book_snapshot.py`
- WatchSet class with rules from §5.2 (including persistence in `data/book_logger/watchset.json` and grace-first eviction)
- Asyncio scheduler with priority queue
- Hook from `book_logger/chain_subscriber.py` to enqueue +1/+3/+10/+30/+60s after each BS event
- 10s tick loop for all tokens in WatchSet
- Pluggable JsonlWriter
- Output: `data/book_logger/book_snapshots/YYYY-MM-DD.jsonl`
- **Done when**: snapshot file shows 5 entries per BS event with `offset_ms_from_received` in {1000, 3000, 10000, 30000, 60000} plus tick rows every ~10s

### Phase 2W: Weather universe lightweight collector

- New file: `book_logger/weather_universe.py`
- Every 15 minutes, query gamma-api for active weather markets using tag + title heuristics from §4.6
- Write one lightweight row per active weather market to `data/book_logger/weather_universe/YYYY-MM-DD.jsonl`
- No write-time dedup in v1: every 15-minute poll writes the full lightweight row; analysis can dedup/downsample later
- Track resolved/closed transitions; after `closed=true` or `resolved=true`, keep polling for 1 hour grace, then remove the market from the active poll set
- Do not call REST `/book` for every weather token; only promote tokens into WatchSet when §4.6 full-book promotion criteria match
- Promotion ranking when more markets qualify than the sample cap: `liquidity_num desc`, then `volume_24h_num desc`, then `market` ascending
- Config knobs:
  - `weather_universe_enabled` default `true`
  - `weather_universe_interval_seconds` default `900`
  - `weather_full_book_sample_cap` default `20`
  - `weather_full_book_min_liquidity` default `3000`
  - `weather_full_book_expiry_hours` default `24`
- Output: `data/book_logger/weather_universe/YYYY-MM-DD.jsonl`
- **Done when**:
  1. 24h run captures active weather markets with non-null `market`, `title`, `tokens`, `volume_total_num`, and `liquidity_num` for ≥95% of rows
  2. No more than `weather_full_book_sample_cap` non-BS weather markets are in WatchSet at once
  3. REST `/book` calls remain bounded by WatchSet size, not by total weather market count
  4. Closed/resolved markets disappear from the active poll set after 1h grace

### Phase 2.5: Volume calibration

- Run Phase 1 + Phase 2 + Phase 2W in production for 24h
- Measure actual disk delta on `data/book_logger/book_snapshots/`, `data/book_logger/bs_events/`, and `data/book_logger/weather_universe/`
- If extrapolated 14-day total > 1 GB (uncompressed), enable optional decimation: skip a tick snapshot when `book_hash` matches the previous snapshot for the same token. This drops idle-token rows on identical books.
- Update §7.1 numbers with measured values before proceeding to Phase 3+

### Phase 3: Trade tape poller (with own-fill side-output)

- New module: `book_logger/trade_tape_poller.py` (separate from both `src/chain_feed.py` and `book_logger/chain_subscriber.py`; not driven by either)
- Per-block `eth_getLogs` against V2 standard + neg-risk exchange addresses, topic filter `[ORDER_FILLED]`, no wallet filter
- Maintain `last_processed_block` watermark on disk (`data/book_logger/trade_tape_watermark.json`); on startup load from disk or fall back to `head - 30` blocks
- For each decoded log:
  - **Trade-tape output**: if `tokenId` is in WatchSet → write a row to `data/book_logger/trade_tape/`
  - **Own-fill side-output**: if `maker == our_proxy or taker == our_proxy` → compute `own_wallet_side`, then emit to `own_fills_queue` regardless of WatchSet membership (used by `copy_decision_log.py` to write `filled` rows with `fill_source: "chain_orderfilled"`)
- **Ring buffer for late-add tokens**: keep the last 30 blocks of decoded logs in memory (regardless of WatchSet match). When WatchSet adds a new token, replay the buffer through the WatchSet filter and write any matching rows. Without this, follower fills in the same block as BS's first touch on a token would be lost (the watermark already advanced past that block before the token entered WatchSet).
- Output: `data/book_logger/trade_tape/YYYY-MM-DD.jsonl` (trade tape) + side queue to copy_decision_log
- **Done when**:
  1. trade tape entries appear for any address (not just BS) on tokens BS has touched, including BS's own fills (which also appear in `bs_events.jsonl` with the same `event_id`)
  2. ring buffer replay verified by adding a synthetic token mid-block and checking prior fills on it appear in trade tape
  3. own-fill side-output emits to `own_fills_queue` for any test fill on `our_proxy` regardless of whether the token was in WatchSet

### Phase 4: Copy decisions logger

- New module: `book_logger/copy_decision_log.py` or thin bot-side adapter exposing `write(event_type, bs_event_id_or_none, source_event_key, **payload)`
- Append-only JSONL — no in-place updates; each lifecycle stage emits a separate row
- Module-level `ACTION_MAP` constant maps every existing production label to `(event_type, decision_path[, skip_reason])` tuples. **Two key types**, looked up in order:
  1. **Exact**: literal string keys (e.g. `"CHAIN_DETECT"`, `"SKIPPED_VWAP"`)
  2. **Prefix**: keys ending with `_` are treated as prefixes (e.g. `"RECONCILE_STALE_BACKFILL_": ("detected", "reconcile", None)` matches `"RECONCILE_STALE_BACKFILL_chain"`, `"RECONCILE_STALE_BACKFILL_ws"`, etc.). Longest-prefix-match wins. This handles the f-string labels existing code emits.
- **Mapping completeness is enforced by a unit test** (`tests/test_action_map.py`):
  1. Walk every `_log_copy_decision` / `_log_vwap_skipped` call site in `src/`. Extract literal-string action arguments AND f-string templates (`f"RECONCILE_STALE_BACKFILL_{source}"` → template `"RECONCILE_STALE_BACKFILL_"`).
  2. For each literal label, assert exact-or-prefix match in `ACTION_MAP`.
  3. For each f-string template, assert the corresponding prefix key exists in `ACTION_MAP`.
  4. Reverse direction: every prefix key in `ACTION_MAP` must have at least one f-string template in production code that emits matching labels (no dead keys).
  5. Test fails if any of the above checks fail. CI / pre-commit blocks merges that introduce unmapped labels.

  Alternative (cleaner but more invasive): refactor the dynamic labels into `ACTION_*` constants imported from `copy_decision_log.py`. Spec leaves the choice to Phase 4 implementer; the test enforces correctness either way.
- Open-order lifecycle: a periodic scanner (every 5 min, can ride along with `_refresh_exposure`) reads pmxt `fetch_open_orders` and emits an `open` row for each still-active order, with `remaining_sh`. On TTL expiry / manual cancel, emits a `cancelled` row.
- Subscribe to `own_fills_queue` (from §6 Phase 3) → emit `filled` rows with `fill_source: "chain_orderfilled"` and top-level `fill_event_id`/`tx_hash`/`log_index` per §4.4 dedup rule. Cross-reference the submitted-order ledger using the §4.4 chain own-fill matching algorithm to attach `pmxt_order_id`, `bs_event_id`, `source_event_key`, `own_wallet_side`, and `match_status`. If a maker-side exact match proves our own hash, populate `own_order_hash`; never treat a taker-side chain `order_hash` as our own order hash.
- Deprecate `data/vwap_skipped.csv` for book-logger analysis (stop using it as the research dataset; existing file kept untouched)
- Output: `data/book_logger/copy_decisions/YYYY-MM-DD.jsonl`
- **Done when**:
  1. Every BS event has at least a `detected` row
  2. Each `evaluated` row is followed either by `skipped`, or by `submitted`. `submitted` is allowed to be terminal in the file (limit order still open at end of window) — no requirement that every submitted have a `filled`. The expected lifecycle is `submitted → ([open]* → filled* → cancelled?)` or `submitted → failed`. Unit test verifies the state machine, not specific row counts.
  3. Mapping unit test passes against current codebase
  4. For any test order that fills via own-wallet `OrderFilled`, a `filled` row appears within 10s of chain confirmation

### Phase 5: WS raw dumper

- New file: `book_logger/book_ws_dumper.py`
- WS connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe with WatchSet asset_ids
- Resubscribe when WatchSet changes
- Append every message verbatim with `_logged_at_ms` prefix
- Output: `data/book_logger/book_ws_raw/YYYY-MM-DD.jsonl`
- **Done when**: 1h dump shows mix of `book` (initial snapshots) and `price_change` events

### Phase 6: Daily backtest tool

- New file: `tools/book_backtest.py`
- Inputs: date range, gate config (multiple variants for grid search)
- For each BS event in range:
  - Load `bs_events.jsonl`, look up the +3s `book_snapshots.jsonl` row
  - Run gate logic against the snapshot
  - If pass: simulate fill using the **limit-walk algorithm in §8** (full / partial / no fill — never assume full fill at any blended VWAP)
  - Look up matching BS exit (sell_avg from same asset_id later events)
  - Record simulated PnL using only filled shares
- Outputs: aggregate stats per gate config, per-trade detail, per-bucket breakdown
- Weather baseline: load `weather_universe.jsonl` to compare BS-touched weather trades against the broader active weather market set by liquidity bucket, expiry window, probability bucket, and final resolution outcome.
- **Done when**: tool produces same numbers as a hand-computed validation example for at least 3 BS round-trips, including one round-trip where the gate passes but the limit-walk yields partial fill (validates the §8 algorithm path)

### Phase 7 (deferred): WS replay backtest

Not in v1. If REST backtest results are inconclusive (e.g. EV very sensitive to which snapshot offset we pick), then build the book engine to replay WS messages and reconstruct book at any timestamp.

### Build order summary

P1 → P2 + P2W → **P2.5 (24h calibration)** → P3 → P4 (parallelizable with P3) → P5 → wait 14 days collecting → P6.

Estimated implementation time: P1 1h, P2 4h, P2W 2h, P2.5 0.5h analysis, P3 3h (more than before — separate poller), P4 2h, P5 2h, P6 4h. Total ~18.5h spread over 2–3 sessions.

## 7. Operational concerns

### 7.1 Disk usage (re-estimated, see Phase 2.5)

Original 6 MB/day book_snapshots estimate was wrong (under by ~7×). Realistic numbers:

All numbers below are **pre-calibration estimates** with row sizes assumed at ~500 B (full-book JSONL rows can be larger if a market has deep books). Phase 2.5 replaces these with measured numbers.

| File | Daily nominal | Daily theoretical max | Notes |
|------|--------------|----------------------|-------|
| `data/book_logger/book_snapshots` | ~43 MB (10 active tokens × 8,640 ticks/day × ~500 B) | ~215 MB (50-token cap) | Largest single contributor; decimation helps if hash unchanged |
| `data/book_logger/bs_events` | ~75 KB | ~150 KB | 50 events/day × 1.5 KB |
| `data/book_logger/weather_universe` | ~5 MB | ~50 MB | Lightweight all-weather metadata, 15-min cadence; no full books |
| `data/book_logger/trade_tape` | ~150 KB | spiky on BS-active hours | All-wallet fills on BS-touched tokens |
| `data/book_logger/copy_decisions` | ~50 KB | ~100 KB | Multiple lifecycle rows per BS event |
| `data/book_logger/book_ws_raw` | ~30 MB | ~150 MB | Same WatchSet, similar update volume |
| **Total** | **~80 MB/day** | **~415 MB/day** | |

14-day window:
- Nominal ~1.1 GB raw, ~275 MB gzipped
- Theoretical max ~5.8 GB raw, ~1.5 GB gzipped

Both fit on the host's `./data/` volume. Phase 2.5 calibration replaces these estimates with measured numbers; if the measured 14-day projection exceeds nominal × 3, enable per-token tick decimation (skip when `book_hash` unchanged) before continuing. If REST `/book` rate usage is high, reduce `weather_full_book_sample_cap` before reducing BS-touched snapshots.

### 7.2 Rotation

Each writer opens the day's file lazily on first write, closes on UTC midnight rollover. No need for logrotate; daily rotation is built into the filename.

### 7.3 Monitoring

Add to existing 30-min Slack portfolio summary or standalone `book-logger` health log:
- `book_logger: snapshots/h={N}, weather_markets/h={W}, ws_msgs/h={M}, errors_24h={K}, events_drops_24h={D}, snapshot_trigger_drops_24h={S}`
- Alert if `snapshots/h < 50` for 1h (probably broken)
- Alert if `weather_markets/h == 0` for 1h while `weather_universe_enabled=true` (gamma-api or classifier broken)
- **Alert if `events_drops_24h > 0`** — `book_logger/chain_subscriber.py` had to drop a raw log because the internal events queue was full. Most likely cause: `events_logger.py` worker thread died or stalled. Investigate immediately; `bs_events` has a hole.
- **Alert if `snapshot_trigger_drops_24h > 0`** — `book_logger/chain_subscriber.py` had to drop a snapshot trigger because `snapshot_trigger_queue` was full. Most likely cause: `book_snapshot.py` poller died or stalled. Snapshots for that BS event are missing.

Add to docker compose logs grep targets so we see:
- Any `book_logger:` warnings
- WS reconnect events
- Any `EVENTS_DROPPED` or `SNAPSHOT_TRIGGER_DROPPED` increments

### 7.4 Disable mechanism

Primary disable mechanism: stop the standalone `book-logger` service (`docker compose stop book-logger`). Optional config flag: `"book_logger_enabled": true` in `config.json`; if implemented, set to `false` and restart only the book-logger service. Bot continues as before because `src/chain_feed.py` is independent.

## 8. Backtest design (for reference)

`tools/book_backtest.py` v1 surface:

```bash
python3 tools/book_backtest.py \
  --start 2026-04-29 \
  --end 2026-05-13 \
  --offset-ms 3000 \                       # which snapshot to use as decision moment
  --gate-config '{"lt_0_05": 1.5, "0_05_to_0_15": 1.0, "0_15_to_0_50": 0.5, "gte_0_50": 0.10}' \
  --copy-pct 1.0 \
  --max-position-usd 1500
```

Outputs:
1. Aggregate: total simulated trades, total invested, total PnL, ROI, win rate, vs BS
2. Per-bucket breakdown: same metrics by BS entry price bucket
3. Per-trade CSV: every simulated trade with entry/exit/PnL for sanity-checking
4. Comparison row: what the actual bot did (from copy_decisions) vs what the simulation says it should have done — this is the validation

**Limit-buy fill model** (used when gate passes, given a `limit_price` and `our_size`):

```
remaining = our_size
cost = 0
for (price_str, size_str) in ascending(asks):
    price = float(price_str); size = float(size_str)
    if price > limit_price:        # past our limit; stop
        break
    take = min(remaining, size)
    cost += take * price
    remaining -= take
    if remaining <= 1e-9:
        break

filled = our_size - remaining
fill_state = "full" if filled >= our_size - 1e-9 \
        else "partial" if filled > 0 \
        else "none"
avg_px = cost / filled if filled > 0 else None
```

A "no fill" outcome (limit below the best ask) is a legitimate result and counts as zero exposure for PnL. A "partial fill" counts only the filled shares against BS's exit price. Do **not** assume full fill at any limit-walk VWAP; that's the bug from §4 review #4.

Grid search variant:
```bash
python3 tools/book_backtest.py --grid-search
```
Iterates over a predefined list of gate configs and outputs a markdown table ranking by ROI.

Validation requirement: For dates where the bot was running, the backtest's simulation of "current production gate" must match `copy_decisions.jsonl` decisions to within 5%. If it doesn't, the backtest model is wrong and we should not trust grid-search recommendations until reconciled.

## 9. Open questions / future work

- **Polymarket API auth for tighter book endpoint**: REST `/book` is public but rate-limited. If we hit limits, switch to authenticated endpoint via existing pmxt credentials.
- **Per-tick sampling cost**: 10s ticks for ~10 active tokens = 86,400 REST calls/day. If rate-limited, fall back to 30s ticks.
- **Full-weather-book expansion**: only consider 10s full-book snapshots for all weather markets after Phase 2.5 proves REST rate and disk headroom. Default remains lightweight `weather_universe` plus bounded full-book promotion.
- **WS book engine (v2)**: when ready, build separate `tools/ws_replay.py` that consumes `book_ws_raw` and reconstructs book state at arbitrary timestamps. Then re-run backtest with finer-grained decision moments.
- **Sell-side simulation**: v1 backtest uses BS's actual exit prices as our exit price. This is optimistic (we may not be able to exit at exactly BS's price either). v2 should also simulate sell-side using bid book at exit time.
- **Held-to-resolution PnL**: for BS positions held past market resolution, look up resolution outcome from gamma-api and compute PnL using $1/$0 settlement values.

## 10. Review Notes

Review time: 2026-04-29T07:49:00Z (2026-04-29 15:49:00 +08:00).
Reviewer: OpenCode / gpt-5.5, based on the current spec plus `src/chain_feed.py`, `src/main.py`, `src/trading.py`, and `src/ws_feed.py`.

All 10 findings accepted. Resolutions applied to spec v1.1:

| # | Finding (summary) | Where resolved |
|---|-------------------|----------------|
| 1 | event_id should be `{tx_hash}:{log_index}`, not just tx hash | §4.1 / §4.2 / §4.3 / §4.4 schema fields |
| 2 | trade tape cannot be built from existing wallet-scoped ChainFeed | §4.3 source paragraph + §5.4 threading + §6 Phase 3 (separate `trade_tape_poller.py`, per-block `eth_getLogs`) |
| 3 | `side` ambiguous (maker order side vs wallet direction) | §4.2 split into `order_side` + `wallet_side`; §4.3 uses `order_side` |
| 4 | limit-buy simulation overstated fills (always full) | §8 added explicit limit-walk algorithm with full/partial/none outcomes |
| 5 | volume estimate ~7× too low | §4.1 estimate revised; §6 Phase 2.5 added (24h calibration); §7.1 totals updated with nominal + worst-case |
| 6 | WatchSet loses 24h grace on restart | §5.2 added persistence; later v1.16 standardizes path to `data/book_logger/watchset.json` |
| 7 | offset-time anchor ambiguous | §4.1 schema split into `event_block_ts_ms` + `event_received_ts_ms` + `snapshot_ts_ms` + `offset_ms_from_received` |
| 8 | copy_decisions enum omitted lifecycle stages used in code | §4.4 split into `event_type` (lifecycle) + `skip_reason` (only for skips); mapping table noted |
| 9 | append-only conflict (in-place vs follow-up rows) | §4.4 explicitly append-only; multiple rows per `bs_event_id`, joined by backtest |
| 10 | `fee_rate_bps` misleading (V2 has fee amount, not rate) | §4.2 / §4.3 use `fee_amount_raw` (string) + `fee_amount_num` (numeric pUSD) |

### Second Review

Review time: 2026-04-29T08:13:19Z (2026-04-29 16:13:19 +08:00).
Reviewer: OpenCode / gpt-5.5, based on spec v1.1 plus current `src/chain_feed.py`, `src/main.py`, and `src/trading.py`.

All 11 findings accepted. Resolutions applied to spec v1.3:

| # | Severity | Finding (summary) | Where resolved |
|---|----------|-------------------|----------------|
| 1 | High | Phase 6 still had `min(limit_price, walking-VWAP)` contradiction | §6 Phase 6 now references §8 limit-walk explicitly; "Done when" requires partial-fill validation case |
| 2 | High | Phase 1 schema can't be filled from current chain_feed event | §6 Phase 1 added "Required `chain_feed` enrichment" listing 5 new fields (`log_index`, `order_side_enum`, `bs_role`, `fee_amount_raw`, `block_ts_ms`) plus name-collision rename |
| 3 | High | `tx_companions` needs same-tx non-BS fills | §4.2 added "Companion-fill source" paragraph; §6 Phase 1 includes synchronous receipt fetch + tx_hash cache |
| 4 | High | `copy_decisions.bs_event_id` mandatory but non-chain paths can't supply it | §4.4 made `bs_event_id` nullable; added `source_event_key` (always set) with format per signal source; backtest fuzzy-join fallback noted |
| 5 | High | `filled` row source ambiguous | §4.4 added `fill_source` enum with priority order: chain_orderfilled > sidecar_immediate > clob_polling > activity_recon; backtest dedups by `(bs_event_id, order_id)` |
| 6 | Medium | `skip_reason` omitted existing labels | §4.4 enum extended (`min_shares`, `min_order`, `size_zero`, `unknown_price`, `reconcile_stale_no_ask`, `reconcile_stale_drift`); Phase 4 must be exhaustive |
| 7 | Medium | `decision_path` omitted real signal sources | §4.4 split into `signal_source` (raw verbatim) + `decision_path` (normalized: chain/ws/reconcile/activity/startup) |
| 8 | Medium | trade_tape poller could miss same-block follower fills | §6 Phase 3 added 30-block ring buffer; replays buffer through filter on WatchSet add |
| 9 | Medium | WatchSet eviction could drop active holdings | §5.2 explicit eviction priority: grace first, active last with `data_incomplete` flag + Slack alert |
| 10 | Medium | §5.5 restart row didn't reflect §5.2 persistence | §5.5 row rewritten with full startup sequence including `data/book_logger/watchset.json` load + 24h drop + /positions merge + watermark restore |
| 11 | Low | §7.1 called nominal "upper bound" | §7.1 renamed to "nominal" + "theoretical max" + explicit "pre-calibration estimates" caveat |

### Third Review

Review time: 2026-04-29T08:42:23Z (2026-04-29 16:42:23 +08:00).
Reviewer: OpenCode / gpt-5.5, based on spec v1.3 plus current `src/chain_feed.py`, `src/main.py`, and `src/trading.py`.

All 9 findings accepted. Resolutions applied to spec v1.5:

| # | Severity | Finding (summary) | Where resolved |
|---|----------|-------------------|----------------|
| 1 | High | `chain_orderfilled` needs own-wallet feed, BS `chain_feed` is wallet-scoped | §4.4 fill_source clarified to use `trade_tape_poller` side-output; §5.4 threading + §6 Phase 3 add own-fill side-output to `own_fills_queue` |
| 2 | High | Phase 1 synchronous RPC blocks copy hot path | §6 Phase 1 moved enrichment off the WS handler; later v1.16 supersedes bot `chain_feed` queues with standalone `book_logger/chain_subscriber.py` + `events_logger.py` |
| 3 | High | Phase 4 done criteria assumed every submitted → filled | §4.4 added `open` periodic lifecycle event_type and clarified `submitted` may be terminal; added Example 4 (TTL-expired open order); §6 Phase 4 rewritten with state-machine validation + open-order scanner |
| 4 | High | Metadata source undefined | new §5.7 token metadata cache (gamma-api `clob_token_ids` lookup, `data/book_logger/token_meta.json` persistence, null-on-miss policy); §6 Phase 1 references it |
| 5 | Medium | decision_path enum/example mismatch | §4.4 example rows now use `decision_path: "chain"` + `signal_source: "chain_fast"` and added Example 3 for non-chain paths |
| 6 | Medium | §5.5 WatchSet full row still said evict-oldest | §5.5 row rewritten to match §5.2 priority (grace first, active eviction → log + alert) |
| 7 | Medium | Fill dedup ambiguous when bs_event_id null | §4.4 dedup rule changed to `(coalesce(bs_event_id, source_event_key), order_id)` |
| 8 | Medium | Phase 4 mapping not exhaustively enforced | §6 Phase 4 added `tests/test_action_map.py` requirement: greps every `_log_copy_decision` / `_log_vwap_skipped` call site, fails if any label unmapped |
| 9 | Low | Phase 2 done criteria still said `offset_ms` | renamed to `offset_ms_from_received` |

### Fourth Review

Review time: 2026-04-29T09:04:54Z (2026-04-29 17:04:54 +08:00).
Reviewer: OpenCode / gpt-5.5, based on spec v1.5 plus current `src/chain_feed.py`, `src/main.py`, and `src/trading.py`.

All 6 findings accepted. Resolutions applied to spec v1.7:

| # | Severity | Finding (summary) | Where resolved |
|---|----------|-------------------|----------------|
| 1 | High | Own-fill matching lacks `order_hash` from V2 OrderFilled topic[1] | `order_hash` added to §4.2 bs_events, §4.3 trade_tape, §4.4 copy_decisions submitted/open/filled/cancelled rows; book_snapshots is NOT modified (joins through `event_id` to bs_events.order_hash, no need to duplicate); §4.4 dedup primary key set in v1.7 (later refined in v1.9 per fifth review #2); §6 Phase 1 enrichment list adds `order_hash = topics[1]` |
| 2 | Medium | bs_events schema lacked metadata fields | §4.2 schema now includes `market`, `title`, `outcome`, `neg_risk` (resolved from §5.7 cache) |
| 3 | Medium | Gamma field naming wrong (`question` not `title`; nested JSON strings) | §5.7 rewritten with verified field mapping: `title=question`, `outcomes`/`clobTokenIds` are JSON-encoded strings inside the response, parallel-list lookup of token → outcome |
| 4 | Medium | Example 1 still had `decision_path: "chain_fast"` | Example 1 updated to `decision_path: "chain"` + `signal_source: "chain_fast"` + added `source_event_key` |
| 5 | Medium | ACTION_MAP unit test couldn't catch dynamic f-string labels | §6 Phase 4 ACTION_MAP supports two key types (exact + prefix-with-trailing-underscore); test extracts BOTH literals and f-string templates from production code and validates in both directions (no unmapped labels, no dead keys) |
| 6 | Low | §5.1 module list stale (missing bs_events_log/token_meta/trade_tape_poller) | §5.1 updated to list all modules (now includes `tests/test_action_map.py` too) |

### Fifth Review

Review time: 2026-04-29T09:29:38Z (2026-04-29 17:29:38 +08:00).
Reviewer: OpenCode / gpt-5.5, based on spec v1.7 plus current `src/trading.py` and `src/chain_feed.py`.

All 5 findings accepted. Resolutions applied to spec v1.9:

| # | Severity | Finding (summary) | Where resolved |
|---|----------|-------------------|----------------|
| 1 | High | Locally computing the submitted own-order hash was infeasible (Python doesn't hold signed struct) | §6 Phase 1 added empirical sub-task; later v1.15 split raw chain `order_hash` from `own_order_hash` and keeps `PMXT_ID_IS_ORDER_HASH` strictly boolean. |
| 2 | High | Dedup `(order_hash, fill_source)` collapsed legitimate partial fills | §4.4 uses two-layer dedup: chain per-fill identity is `(tx_hash, log_index)`; non-chain identity prefers `pmxt_order_id`; per-order aggregation groups confident rows by `pmxt_order_id`. |
| 3 | Medium | `log_index` encoding ambiguous (hex vs decimal) | §4.2 added explicit "Encoding rules" section: `tx_hash` lowercase, `log_index` decimal int, `event_id = f"{tx}:{idx}"`. §4.2 / §4.3 schema annotations made type explicit. §6 Phase 1 enrichment list and unit test enforce normalization at ingest. |
| 4 | Medium | queue full behavior undefined | §6 Phase 1 specifies bounded queues + `try/except queue.Full`; later v1.16 uses `EVENTS_DROPPED_COUNTER` in standalone `book_logger/chain_subscriber.py` rather than bot `chain_feed` queues. |
| 5 | Low | Fourth-review resolution table incorrectly claimed §4.1 got `order_hash` | Fourth-review row #1 corrected: book_snapshots NOT modified, joins through `event_id` to `bs_events.order_hash` (no duplication needed). |

### Sixth Review

Review time: 2026-04-29T09:50:29Z (2026-04-29 17:50:29 +08:00).
Reviewer: OpenCode / gpt-5.5, based on spec v1.9 plus current `src/trading.py` and `src/chain_feed.py`.

All 5 findings accepted. Resolutions applied to spec v1.11:

| # | Severity | Finding (summary) | Where resolved |
|---|----------|-------------------|----------------|
| 1 | High | §4.4 still claimed `order_hash` could be "computed locally from signed struct" (contradicting Phase 1) | §4.4 schema description now treats `order_hash` as raw maker-order provenance and uses `own_order_hash` only when our submitted hash is verified known. "compute locally" language removed. |
| 2 | Medium | `filled` row example nested `tx_hash` inside `fill`, omitted `log_index` | Added top-level `fill_event_id = "{tx_hash}:{log_index}"` + top-level `tx_hash` and `log_index` on chain_orderfilled rows; example updated to match dedup rule |
| 3 | Medium | Non-chain dedup unsafe when `order_hash` null at sidecar_immediate time | §4.4 dedup rule now prefers `pmxt_order_id`, then `own_order_hash`, then raw `order_hash`; cross-source dedup may backfill `own_order_hash` only for maker-side own fills |
| 4 | Medium | Phase 4 wording said "matching `order_id`" but schema uses `pmxt_order_id` / `order_hash` | Phase 4 own-fill cross-reference text updated to use both ids explicitly + describes backfill flow when `PMXT_ID_IS_ORDER_HASH=false` |
| 5 | Low | §4.2 companion-fill paragraph wording could be misread as hot-path RPC | Paragraph clarified: receipt fetch is in the enrichment worker thread; later v1.16 names this `book_logger/events_logger.py`, fed by standalone `chain_subscriber.py` |

### Seventh Review

Review time: 2026-04-29T10:02:23Z (2026-04-29 18:02:23 +08:00).
Reviewer: OpenCode / gpt-5.5, based on spec v1.11 plus current `src/chain_feed.py` and `src/trading.py`.

All 6 findings accepted. Resolutions applied to spec v1.13:

| # | Severity | Finding (summary) | Where resolved |
|---|----------|-------------------|----------------|
| 1 | High | `order_hash` is maker-order-only; doesn't identify our taker fills | §4.4 `order_hash` semantic clarified explicitly: `topics[1]` = maker's hash; our taker fills emit counterparty hashes. v1.15 adds the matching algorithm that attaches `pmxt_order_id`; `order_hash` is secondary metadata only valid as our own hash when `is_own_maker == true`. |
| 2 | High | Snapshot trigger could be delayed by event enrichment | §5.3 architecture + §6 Phase 1 now keep fast snapshot scheduling separate from slow enrichment; later v1.16 places both queues inside standalone `book_logger/chain_subscriber.py`, not bot `src/chain_feed.py`. |
| 3 | Medium | sidecar_immediate aggregates not 1:1 with chain fills | §4.4 sidecar_immediate marked **provisional** with replacement-on-chain-arrival semantics: chain rows arriving within 60s for same `pmxt_order_id` REPLACE the sidecar aggregate (not 1:1 dedup). |
| 4 | Medium | activity_recon source_seq assumed unique id | §4.4 source_seq priority chain documented: `transactionHash → txHash → hash → weak:{asset_id}:{own_wallet_side}:{ts}:{size}` with `key_strength` field; weak rows raise `data_uncertain` flag in backtest. |
| 5 | Medium | Phase 1 test order may never fill | §6 Phase 1 sub-task rewritten: marketable order (limit = `best_ask × 1.02`); also explained that taker fills on test order can't directly verify `pmxt.id == topics[1]` — added a second maker-side test option, with conservative fallback `PMXT_ID_IS_ORDER_HASH = False` if test inconclusive. |
| 6 | Low | spec used `immediate_fills` but pmxt code uses `fills` | §4.4 source_seq defs use `fills` directly; normalization layer note added. |

### Eighth Review

Review time: 2026-04-29T10:15:34Z (2026-04-29 18:15:34 +08:00).
Reviewer: OpenCode / gpt-5.5, based on spec v1.13 plus current `src/chain_feed.py` and `src/trading.py`.

All 6 findings accepted. Resolutions applied to spec v1.15:

| # | Severity | Finding (summary) | Where resolved |
|---|----------|-------------------|----------------|
| 1 | High | Chain own-fill rows need an ambiguity-safe path to `pmxt_order_id` | §4.4 adds the submitted-order ledger and exact/fuzzy/ambiguous/unmatched matching algorithm; per-order aggregation excludes unmatched/ambiguous rows |
| 2 | High | `snapshot_trigger_queue` payload too small for snapshot schema | §4.1 makes `event_block_ts_ms` and metadata nullable on hot-path snapshots; §6 Phase 1 expands the trigger payload with decoded BS fields and forbids block-ts RPCs on the subscriber hot path |
| 3 | Medium | Own-fill matching needs actual wallet side | §4.3 / §4.4 add `own_wallet_side`; §5.4 / §6 Phase 3 require `own_fills_queue` to compute it; matching uses `own_wallet_side`, not maker `order_side` |
| 4 | Medium | `PMXT_ID_IS_ORDER_HASH` bool/string mismatch | §4.4 and §6 Phase 1 split this into strict bool `PMXT_ID_IS_ORDER_HASH` plus enum `PMXT_ID_HASH_STATUS` |
| 5 | Medium | §5.4 / §5.6 understated queue ownership | Initially resolved in v1.15 by naming both queues; later v1.16 moves those queues into standalone `book_logger/chain_subscriber.py` and keeps `src/chain_feed.py` unchanged |
| 6 | Low | §4.2 / §4.3 `order_hash` comments implied universal own-fill join | §4.2 / §4.3 comments now define it as maker-order hash; §4.4 explains own-order validity only when `is_own_maker == true` |

### Standalone Service Update

Update time: 2026-04-30.

Standalone adjustments applied to spec v1.16:

| # | Change | Where resolved |
|---|--------|----------------|
| 1 | Book-logger outputs live under `data/book_logger/` instead of top-level `data/` paths | §4 schema headings, §6 phase outputs, §7 disk usage |
| 2 | `book-logger` owns its own `chain_subscriber.py`; `src/chain_feed.py` remains bot-only and must not receive logger/snapshot queues | §3 decision #8, §5.3 integration model, §5.4 threading, §5.6 non-changes, §6 Phase 1 |

### Weather Universe Update

Update time: 2026-04-30.

Weather-universe adjustments applied to spec v1.17:

| # | Change | Where resolved |
|---|--------|----------------|
| 1 | Collect all active weather markets as lightweight metadata/probability/liquidity rows | §3 decision #9, §4.6 schema, §6 Phase 2W |
| 2 | Do not collect 10s full-book snapshots for every weather market by default; promote only BS-touched or bounded samples | §2 non-goals, §4.6 promotion criteria, §5.2 WatchSet rules, §7.1 rate/disk guidance |

Conclusion: v1.17 is ready to implement once §11 approvals are checked.

## 11. Approvals

- [ ] User has reviewed schemas in §4
- [ ] User has reviewed implementation phases in §6
- [ ] User has approved disk usage budget in §7.1
- [ ] User has confirmed go-ahead

Once all checked, build starts at Phase 1.

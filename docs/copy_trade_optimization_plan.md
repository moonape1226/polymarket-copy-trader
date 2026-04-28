# Copy Trade Optimization Plan

## Goals

- Follow target trades as fast as possible.
- Reduce slippage and avoid stale fills.
- Preserve accurate execution records across restarts.

## 1. Latency Improvements

- Keep reconcile as a safety backfill path only. Do not let old reconcile signals chase price: add age gates, price-drift gates, max notional caps, and tight-limit-only behavior for stale signals.
- Initial reconcile gate values:

| Gate | Value | Behavior |
|---|---:|---|
| Stale age | `>60s` | Stop treating reconcile as a normal entry path. |
| Price drift | `cur_ask > reference_price * 1.05` | Skip stale reconcile entirely. Use `bs_avg` when known; use `detection_price` when BS price is unknown. |
| Tight limit | `min(bs_avg * 1.02, cur_ask * 0.98)` | Submit only an opportunistic buy, never chase the ask. |
| Tight-limit TTL | Bucket TTL | Use the same bucket TTL table as normal buys. |
| Stale max notional | `min_trade_usd * 2` | Cap stale reconcile size even when BS size is large. |

- Reconcile gates must run after `BS_SELL_REBUY_COOLOFF`. If BS sold within the 60s cooloff, skip reconcile first; after cooloff expires, apply stale age and price-drift gates before any buy.
- Lower `chain_quiesce_seconds` from the current 1.5s default to a smaller value such as 0.3-0.7s, then measure missed multi-fill aggregation risk.
- Prefetch Gamma metadata as soon as a chain bucket is first created, so dispatch does not block on metadata lookup at flush time.
- Add a WS fast-lane for buy signals: use a short split-detection grace period, then dispatch instead of waiting for the full batch window.
- Track chain, WS, activity, and reconcile latency separately in `order_metrics.csv` / `copy_decisions.csv` so latency regressions are visible.

## 2. Slippage Improvements

- Add a short bucket-specific entry TTL for buy limit orders and cancel unfilled entries quickly instead of leaving GTC orders on book for minutes.
- Use one bucket table for buy TTL, limit slip, and VWAP tolerance:

  | BS price bucket | Entry TTL | Bucket slip / VWAP tolerance |
  |---|---:|---:|
  | `<0.05` | `30s` | `40%` |
  | `0.05-0.15` | `15s` | `25%` |
  | `0.15-0.50` | `10s` | `15%` |
  | `>=0.50` | `5-10s` | `5%` |

- Use orderbook depth before placing buys. Estimate VWAP for the intended size and skip the main-path order if `VWAP > BS avg * (1 + bucket_slip)` using the same bucket table.
- Main path should skip rather than shrink, because shrinking silently changes the `copy_percentage` contract. Stale reconcile may shrink down to a minimum viable opportunistic size because it is already a backfill path, not primary copying.
- Replace the two-level slip configuration with the bucket table above so limit price, VWAP acceptance, and TTL stay consistent.
- Make stale reconcile signals use stricter pricing than fresh chain or WS signals.
- Separate submitted orders from actual fills. Do not record a limit buy as fully copied until fill status confirms it.

## 3. Restart-Safe Execution Records

- Do not put SQLite writes in the hot path. Keep copy execution in memory and persist asynchronously after decisions, submissions, and fills.
- Defer full durable state until the order lifecycle is better understood from latency and fill metrics. Building SQLite first risks locking in the wrong schema.
- Add durable state, preferably SQLite, for pending orders, processed events, BS cost basis, copy rates, and order lifecycle.
- Persist processed idempotency keys for chain events and activity records to avoid duplicate execution after restart.
- On startup, query the last 5-10 minutes of target `/activity` to cover downtime gaps, especially BS sells that happened while the bot was offline and are invisible in the post-restart position baseline.
- Rehydrate both open buys and open sells on startup. Orphan cleanup already covers open buys; open sells still need TTL fallback state restored.
- Track order lifecycle explicitly: `detected`, `submitted`, `partially_filled`, `filled`, `cancelled`, `expired`, and `failed`.
- Persist BS cost basis instead of relying only on recent activity backfill.
- Reconcile CSV logs from durable state so `bot_trades.csv` represents fills, while `copy_decisions.csv` represents decisions and submissions.

## Suggested Priority

| Phase | Proposal | Why |
|---:|---|---|
| 1 | `CTP-003` fill-vs-submit plus latency metrics | Without metrics, later changes cannot be measured. |
| 2 | `CTP-001A` buy TTL | Directly addresses stale GTC entry fills and can ship independently. |
| 3 | `CTP-005` open-sell TTL rehydration | Cheap restart-safety fix for stuck maker sells without introducing SQLite. |
| 4 | `CTP-001B` reconcile stale gates | Tune after metrics show stale reconcile distribution and impact. |
| 5 | `CTP-002` VWAP/depth gate | Add after TTL and metrics are stable, because it adds hot-path latency. |
| 6 | `CTP-004` durable state | Design after observed lifecycle data; SQLite must stay off the hot path. |

Backlog proposals:

- `CTP-006` sell-side maker TTL policy review.
- `CTP-007` unknown-price signal size scaling.

## Discussion Notes

- Treat the initial thresholds as configurable first-pass defaults, not permanent constants.
- The stale reconcile price-drift gate `cur_ask > bs_avg * 1.05` is directionally right, but low-price markets may need an absolute tolerance such as `max(5%, $0.002)` to avoid skipping valid dust-price moves.
- The stale max notional cap `min_trade_usd * 2` is intentionally conservative. Log skipped stale reconcile opportunities so we can see if it rejects too many useful catch-ups.
- Entry TTL cancellation must not create an automatic re-post loop. After a buy limit expires, only a new BS trade, chain event, or confirmed activity signal should cause another order.
- VWAP checks should use the final capped `our_size`, not the original BS size, so low-prob order caps and exposure limits are reflected before book-depth validation.
- Startup downtime lookback should not blindly replay buys. First pass should focus on sells/exits for assets we hold, because missed offline sells are invisible in the post-restart position baseline.
- Durable SQLite state stays last, but lightweight CSV metrics for latency, submit/fill status, and stale-signal skips should happen earlier because they validate the preceding changes.

## Proposal Assessment

### CTP-001A: Buy TTL

- Readiness: high. This can ship independently from reconcile gates.
- Trade-off: shorter TTL reduces late bad fills but increases missed entries. Bucket TTLs should be locked at order placement and should not change mid-attempt.
- Dependency: none beyond current pending-order tracking.
- Decision: after TTL expiry, do not auto-repost. Only a new BS trade, chain event, or confirmed activity signal can trigger another buy.
- Self-logging spec (so TTL can be evaluated without waiting for full `CTP-003`): on every TTL outcome, log `placed_at`, `expires_at`, `cancel_reason`, `signal_source`, `signal_age_at_placement`, and `bs_holds_at_expiry` (`true` / `false` / `unknown`). Three buckets to monitor:
  - BS still holds → we missed the entry → TTL likely too short
  - BS already exited → we cancelled correctly → TTL is doing its job
  - BS partially holds → record verbatim, treat as gray
- Implementation location for `bs_holds_at_expiry`: the BS positions cache lives in `main.py` (`wallet_states[BS]`), TTL expiry fires in `trading.py`. Do not have `trading.py` make a fresh `/positions` API call at expiry — that adds a real network round-trip on every cancel. Instead, `main.py` exposes a state-backed checker to `TradingModule`; the value can be up to one poll cycle stale and returns `unknown` if a wallet has not initialized.

### CTP-001B: Reconcile Stale Gates

- Readiness: medium-high. Gate design is concrete, but thresholds should be validated by metrics.
- Trade-off: conservative gates reduce high-slippage catch-up buys but may miss legitimate late fills.
- Dependency: preferably after `CTP-003`, so stale age distribution, skip counts, and post-skip outcomes can be measured.
- Unknown-price branch: when `bs_avg` or BS price is unavailable, price-drift checks must use `detection_price` as the reference, otherwise the gate silently disables itself.
- `detection_price` usage scope (binding for all CTPs): allowed for (1) reconcile gate fallback reference, (2) `unknown_price_max_ask` filter trigger, (3) VWAP reference in CTP-002 **only when BS avg is unknown** and the row is tagged `reference_source=detection_price`. Forbidden for: BS cost basis updates, bot exposure accounting, P&L attribution. Treat it as a current-ask risk reference, never as a stand-in for BS fill price. Whenever `detection_price` is used as a reference, the resulting log row must carry `reference_source=detection_price` so analysis cannot mistake it for `reference_source=bs_avg`.
- TTL consistency: stale reconcile tight-limit orders should use the same bucket TTL table as normal buys, not a separate fixed 10-15s TTL.

### CTP-002: VWAP / Depth Gate

- Readiness: medium. The core missing decision is skip vs shrink.
- Trade-off: skip is explicit and preserves `copy_percentage`; shrink is less predictable and changes effective sizing.
- Decision: main copy path skips when VWAP exceeds tolerance. Stale reconcile may shrink to a minimum viable opportunistic size.
- Dependency: should follow `CTP-003` metrics and `CTP-001A` TTL. VWAP fetch adds a hot-path API call, so its added latency must be measured.
- Risk: the low-price bucket currently allows high slip. A thin book can still pass VWAP tolerance even with bad tail prices, so bucket thresholds need monitoring before being treated as final.
- Skipped-trade observation schema (required, not optional). First version (Phase 1, ships with the gate): append a row to `vwap_skipped.csv` with `asset_id`, `condition_id`, `bs_size`, `bs_avg_price`, `our_vwap_estimate`, `bucket`, `bucket_tolerance`, `reference_source` (`bs_avg` or `detection_price`), `skip_reason`, `skipped_at`. The skip row alone does not block the gate from shipping. Phase 2 (separate resolver job, runs as cron): backfill `market_resolved_price` and `missed_pnl` after market resolution. Without the Phase 2 backfill, `vwap_skipped_usd` alone cannot tell us whether the bucket thresholds are too tight or too loose — we need post-resolution PnL of the skipped BS trades to tune them.

### CTP-003: Fill-vs-Submit and Latency Metrics

- Readiness: high.
- Role: diagnostic, not direct execution optimization. It should move earlier because it is required to verify later changes.
- Dependency: independent; can run in parallel with TTL work.
- **Minimal scope (binding for Phase 1):** logging-only CSV changes — no new chain-feed subscriptions, no schema migrations, no SQLite. First version writes submit/failure/immediate-fill timing to `order_metrics.csv` and leaves `bot_trades.csv` unchanged until an authoritative fill source exists. Anything that requires extending `ChainFeed` to observe our proxy wallet is **not** in Phase 1; if that work exceeds 1 day, ship the minimal version and let CTP-001A proceed.
- Fill source policy (target, not Phase 1): own-wallet chain `OrderFilled` as primary, self `/activity` as secondary, open-order polling/set-diff as final reconciliation. Phase 1 starts from the bottom of this list and works up as time allows.
- Logging: first pass writes submit/failure/immediate-fill timing to `order_metrics.csv` rather than widening `copy_decisions.csv`. `bot_trades.csv` keeps its current semantics until authoritative fill confirmation is added; confirmed fills should later include `confirmed_by` either in `bot_trades.csv` or in a fill-specific lifecycle log.

### CTP-004: Durable State

- Readiness: intentionally deferred.
- Trade-off: durable state solves restart gaps, but building SQLite before lifecycle shape is known will likely create the wrong schema.
- Dependency: wait for `CTP-003` metrics to run long enough to show actual lifecycle distribution.
- Boundary: SQLite writes must be async/off-hot-path. Execution decisions remain in memory.

### CTP-005: Open-Sell TTL Rehydration

- Readiness: high.
- Scope: single-file CSV rehydration for open maker sells, not full durable state.
- Trade-off: small extra state file closes a concrete restart gap without committing to a database schema.
- Implementation sketch: write `open_sells.csv` as **append-only** with two event types — `placed` row on maker sell placement (`asset_id`, `order_id`, `placed_at`, `ttl`, `shares`, `market_id`, `is_profit`, `bs_sell_tx_hash`), `cleared` row when filled/cancelled/fallback completes (`asset_id`, `order_id`, `cleared_at`, `cleared_reason`). Active set is derived on startup by reducing events, never by mutating rows in place. A torn write during restart can then only drop a trailing partial line, not corrupt earlier state.
- Restart behavior (sequenced, do not skip steps):
  1. Reduce `open_sells.csv` events into an active set keyed by `order_id`.
  2. For each active row, call `fetch_open_orders()` to confirm the order is still live on CLOB.
  3. Call `fetch_positions()` to get our current shares for that `asset_id`.
  4. If order live and shares present → rehydrate timer with `remaining = max(0, ttl - (now - placed_at))`.
  5. If order gone and shares gone → was filled while offline. Append a `cleared` event (`reason=filled_offline`).
  6. If order gone but shares present → was cancelled (TTL expired offline, or external cancel). **Do not blindly market-sell.** Re-evaluate at current price like a fresh sell signal — current cost basis, current price, current size cap.
  7. If `remaining == 0` (TTL would have expired offline) and order still live → cancel the live order, then re-evaluate as in step 6. Do not auto-fire the market fallback.
- Idempotency key for sell paths: `(asset_id, side, tx_hash)` for `/activity`-derived events, `(asset_id, side, tx_hash, log_index)` for raw chain events. Side disambiguates BUY+SELL in the same tx (e.g., merge); `log_index` disambiguates multi-fill batched into one tx. Second-level timestamps must not be used as the primary key.
- `/activity` field name: verify on live data before relying on it. The field may be `transactionHash`, `tx_hash`, `hash`, or absent in some rows. If missing, fall back to a weak key `(asset_id, side, timestamp_seconds, size)` and tag the dedup row `key_strength=weak`. Weak keys must be permissive (prefer to under-deduplicate and rely on idempotent execution downstream rather than over-deduplicate and miss a real sell).
- The downtime `/activity` lookback (line 52) and rehydrated open-sell set must both consult these keys before dispatching a new sell — covered keys are skipped.

### CTP-006: Sell-Side Maker TTL Policy

- Readiness: low priority.
- Concern: profit/loss TTL currently depends on BS sell price vs BS cost basis, but our cost basis can differ from BS. A BS profit exit can be our loss exit, and the TTL policy may wait too long.
- Decision: record as backlog; review after entry-side losses are controlled.

### CTP-007: Unknown-Price Signal Size Scaling

- Readiness: low priority.
- Concern: unknown-price buys currently become full copy or full skip. A reduced-size path could preserve signal value while limiting risk.
- Trade-off: size scaling adds another implicit sizing layer and must be visible in logs.
- Decision: backlog until metrics show unknown-price skip/copy outcomes.

### Additional Challenges

- Moving `CTP-003` first is right only if the first version is lightweight. If it tries to solve authoritative fill lifecycle fully, it will delay the direct GTC slippage fix. First pass should log submit timing, decision source, stale age, TTL expiry, and available fill confirmation, then iterate.
- Own-wallet chain `OrderFilled` as the primary fill source likely needs either extending `ChainFeed` to observe our proxy wallet or adding a second fill feed. That is not free; `CTP-003` should not block `CTP-001A` if this feed takes longer than expected.
- `CTP-001A` buy TTL can be shipped before full metrics if it logs enough locally: `placed_at`, `expires_at`, `cancel_reason=entry_ttl`, source signal age, and whether BS still holds afterward. Otherwise we will not know whether TTL is preventing bad fills or just increasing missed entries.
- `CTP-005` CSV rehydration must be append-only or atomic-write safe. A half-written `open_sells.csv` during container restart could rehydrate the wrong order state. Prefer append events (`placed`, `cleared`) and derive active sells on startup rather than mutating rows in place.
- Downtime sell recovery and open-sell rehydration can double-act on the same asset after restart. Rehydrated open sell orders should be checked before replaying recent BS sells from `/activity`, and both paths need an idempotency key.
- VWAP skip as the main path is cleaner than shrink, but it can create systematic under-copy in thin books. The metric plan should explicitly report `vwap_skipped_usd` and later PnL of skipped BS trades, otherwise skip quality cannot be evaluated.
- Unknown-price fallback using `detection_price` is necessary, but `detection_price` may be a current ask rather than BS fill. Treat it as a risk reference only, not true cost basis.

### Open Concerns from Review

- **Quiesce 0.3-0.7s split-fill risk.** Lowering `chain_quiesce_seconds` may split a real BS multi-fill (dust + main fill at +0.5s) into two dispatches. The second dispatch re-evaluates `max_position_usd` and exposure caps with the first dispatch's pending in flight, so cap math must read pending cost (already merged at `trading.py:373-379`) rather than only on-chain shares. Verify before lowering.
- **WS fast-lane vs split-detection vs chain merge filter.** All three look at the same `(YES, NO, conditionId)` window but with different timeouts. Define a single split/merge observation window (e.g., 2s grace) shared by WS fast-lane dispatch, batch-flush split detection (`main.py:637-663`), and chain merge filter (`main.py:310-326`); otherwise fast-lane can dispatch before NO arrives and bypass the split filter.
- **Fill-vs-submit truth source.** "Filled" needs a concrete signal: own-wallet `OrderFilled` chain events, polled `fetch_open_orders` set-difference, or `/activity` self-trades. They have different latencies (chain ~1s, /activity ~5-30s, open-orders polling = polling rate). Pick one as authoritative for `bot_trades.csv` and document the lag.
- **BS cost basis persistence vs /activity backfill.** §3.5 persists our own observed BS cost; current code backfills from BS `/activity` on startup. These can disagree if we missed events. Decide: (a) persisted value as cache, reconciled against /activity each startup, or (b) replace /activity backfill entirely and accept gap risk. Default should be (a).
- **Bucket cross-over mid-flight.** A BS-price bucket lookup at order placement defines TTL and slip. If BS price moves from `$0.04 → $0.06` while our order sits, the bucket changes (`<0.05` → `0.05-0.15`). Lock the bucket at placement; re-evaluate only on re-post triggered by a new BS signal. Avoids inconsistent TTL/slip during a single attempt.
- **Latency metric anchors undefined.** §1.5 names four sources but not the time anchors. Suggested: `t0` = BS tx block timestamp (chain) or BS `/activity` `timestamp` field; `t1` = our `_dispatch` call; `t2` = our submit ack from sidecar; `t3` = our fill confirmation. Log `t1-t0` (detection), `t2-t1` (dispatch), `t3-t2` (fill) so each stage is independently visible.
- **Open-sell TTL on restart.** §3.3 rehydrates open sells but does not say whether the TTL clock continues from original placement or resets. Persisting `placed_at` and computing `remaining = max(0, ttl - (now - placed_at))` keeps semantics; resetting effectively extends sells beyond `sell_maker_ttl_loss=15s` and breaks loss-cut behavior.
- **Lifecycle states vs existing CSV semantics.** `gtc_cancelled.csv` already records cancellations with reasons. The new `cancelled / expired / failed` states should map onto and replace that file rather than duplicate it; otherwise we have two sources for the same event.
- **Reconcile + VWAP interaction.** Stale reconcile is tight-limit-only and can skip VWAP depth check (it's by definition opportunistic). Non-stale reconcile (≤60s) should still pass through VWAP like fresh chain/WS signals. State this explicitly so implementation does not blanket-skip VWAP for the entire reconcile path.

### Review Response

- **Quiesce risk is not only cap math.** Lowering `chain_quiesce_seconds` can split one BS multi-fill into multiple dispatches. The buy path cancels an existing pending buy before redispatch, so the second partial fill can replace the first pending order instead of adding to it. That can create under-copy as well as over-copy, depending on timing. Any quiesce reduction needs a test for multi-fill aggregation and redispatch behavior.
- **Prefer one signal aggregator over duplicated filters.** A shared split/merge observation window is directionally right, but separate implementations in WS, batch, and chain paths will drift. The cleaner target is one small signal aggregator that all fast paths feed before dispatch.
- **Fill truth source should be layered, not singular.** Own-wallet chain `OrderFilled` should be the primary fill source because it is fastest and closest to execution. Open-order reconciliation and self `/activity` should be secondary checks for missed chain events, partial fills, and restart recovery.
- **Persist BS trade events, derive cost basis.** Persisted aggregate cost basis is useful as a cache, but the durable source should be the sequence of observed BS trade events. If a missed event is later discovered from `/activity`, derived cost basis can be corrected; an aggregate-only store is harder to repair.
- **Latency anchors need source and receive timestamps.** Record both external timestamps (`block_timestamp`, `/activity.timestamp`) and local timestamps (`received_at`, `dispatch_at`, `submit_ack_at`, `fill_confirmed_at`). Do not compare chain block time, activity time, and local receive time without labeling the source.
- **Do not immediately replace CSV outputs.** Keep `gtc_cancelled.csv` while lifecycle logging/table is introduced. The new lifecycle record can generate or validate the old CSV first; deprecate the CSV only after both agree for a monitoring window.
- **Keep the accepted items.** Open-sell TTL should continue from original `placed_at`, price buckets should be locked for a single order attempt, and non-stale reconcile must pass through the same VWAP checks as fresh chain/WS signals.

| ID | Topic | Owner | Status | Decision / Next Step | Evidence |
|---|---|---|---|---|---|
| CTP-001A | Buy TTL | Open | proposed | Ship early with bucket TTLs and no automatic repost. | 24h audit showed stale GTC/high-slippage entry anomalies. |
| CTP-001B | Reconcile stale gates | Open | proposed | Implement after metrics; unknown price uses `detection_price` reference. | Reconcile latency averaged high in 24h audit. |
| CTP-002 | VWAP/depth gate | Open | proposed | Main path skips; stale reconcile may shrink opportunistically. | Prevents immediate bad-price fills that TTL alone cannot stop. |
| CTP-003 | Fill-vs-submit and latency metrics | Open | proposed | Do first, but keep initial version lightweight and CSV-based. | Needed to validate TTL, reconcile, and VWAP effects. |
| CTP-004 | Durable state | Open | deferred | Revisit after lifecycle shape is validated; SQLite must stay off hot path. | Schema is premature until fill lifecycle is observed. |
| CTP-005 | Open-sell TTL rehydration | Open | proposed | Add append-only CSV state before full durable DB. | Restart can lose maker-sell TTL fallback state. |
| CTP-006 | Sell-side maker TTL policy | Open | backlog | Review after entry-side losses are controlled. | Our cost basis can differ from BS cost basis. |
| CTP-007 | Unknown-price size scaling | Open | backlog | Revisit after unknown-price metrics show outcome quality. | Current behavior is full copy or full skip. |

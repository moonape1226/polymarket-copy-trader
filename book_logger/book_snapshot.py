"""
REST /book poller — Phase 2.

Two trigger sources:
1. snapshot_trigger_queue (from chain_subscriber): 5 snapshots per BS event at
   +1/+3/+10/+30/+60s relative to event_received_ts_ms
2. periodic 10s tick across the WatchSet

Output: data/book_logger/book_snapshots/YYYY-MM-DD.jsonl
Spec: docs/book_logger_spec.md §4.1 + §6 Phase 2.
"""

import datetime
import heapq
import json
import logging
import os
import queue
import threading
import time
from typing import Any, Dict, Optional

import requests

from token_meta import META
from watch_set import WatchSet

logger = logging.getLogger(__name__)

_CLOB_BOOK_URL = "https://clob.polymarket.com/book"
_DATA_DIR = os.environ.get("BOOK_LOGGER_DATA_DIR", "/data/book_logger")
_SNAPSHOTS_DIR = os.path.join(_DATA_DIR, "book_snapshots")
_TICK_INTERVAL_S = 10
_EVENT_OFFSETS_MS = (1000, 3000, 10000, 30000, 60000)
_FETCH_TIMEOUT_S = 4

_write_lock = threading.Lock()


def _output_path(ts_ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
    return os.path.join(_SNAPSHOTS_DIR, dt.strftime("%Y-%m-%d.jsonl"))


def _write_row(row: Dict[str, Any]) -> None:
    path = _output_path(row["ts_ms"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with _write_lock:
        with open(path, "a") as f:
            f.write(line)


def _fetch_book(token_id: str):
    t0 = time.time()
    try:
        r = requests.get(
            _CLOB_BOOK_URL,
            params={"token_id": token_id},
            timeout=_FETCH_TIMEOUT_S,
        )
        latency_ms = int((time.time() - t0) * 1000)
        if not r.ok:
            return None, latency_ms, "REST_FAILED"
        return r.json(), latency_ms, "REST"
    except Exception:
        return None, int((time.time() - t0) * 1000), "REST_FAILED"


def _depth_usd(side_list: list) -> Optional[float]:
    try:
        return sum(float(x.get("price", 0)) * float(x.get("size", 0)) for x in side_list)
    except Exception:
        return None


def _best(side_list: list, ascending: bool) -> Optional[float]:
    if not side_list:
        return None
    try:
        sorted_levels = sorted(side_list, key=lambda x: float(x.get("price", 0)), reverse=not ascending)
        return float(sorted_levels[0].get("price", 0))
    except Exception:
        return None


def _build_row(token_id: str, book: Optional[dict], fetch_latency_ms: int,
               fetch_method: str, trigger: str, event_meta: Optional[dict]) -> Dict[str, Any]:
    snap_ts_ms = int(time.time() * 1000)
    snap_ts_iso = datetime.datetime.fromtimestamp(snap_ts_ms / 1000, tz=datetime.timezone.utc) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")

    asks = (book or {}).get("asks") or []
    bids = (book or {}).get("bids") or []
    best_ask = _best(asks, ascending=True)
    best_bid = _best(bids, ascending=False)
    mid = (best_ask + best_bid) / 2 if (best_ask is not None and best_bid is not None) else None
    spread_pct = ((best_ask - best_bid) / mid) if (mid and mid > 0 and best_ask is not None and best_bid is not None) else None

    meta = META.get(token_id) or {}

    em = event_meta or {}
    return {
        "ts": snap_ts_iso,
        "ts_ms": snap_ts_ms,
        "trigger": trigger,
        "event_id": em.get("event_id"),
        "offset_ms_from_received": em.get("offset_ms", 0) if event_meta else 0,
        "event_block_ts_ms": em.get("event_block_ts_ms"),
        "event_received_ts_ms": em.get("event_received_ts_ms"),
        "snapshot_ts_ms": snap_ts_ms,
        "fetch_latency_ms": fetch_latency_ms,
        "fetch_method": fetch_method,
        "bs_order_side": em.get("bs_order_side"),
        "bs_wallet_side": em.get("bs_wallet_side"),
        "bs_size": em.get("bs_size"),
        "bs_price": em.get("bs_price"),
        "bs_role": em.get("bs_role"),
        "market": meta.get("market"),
        "asset_id": token_id,
        "outcome": meta.get("outcome"),
        "neg_risk": meta.get("neg_risk"),
        "title": meta.get("title"),
        "timestamp": (book or {}).get("timestamp"),
        "hash": (book or {}).get("hash"),
        "asks": asks,
        "bids": bids,
        "best_ask_num": best_ask,
        "best_bid_num": best_bid,
        "mid_num": mid,
        "spread_pct_num": spread_pct,
        "ask_depth_usd_num": _depth_usd(asks),
        "bid_depth_usd_num": _depth_usd(bids),
        "ask_levels": len(asks),
        "bid_levels": len(bids),
    }


class BookSnapshotPoller:
    def __init__(self, snapshot_trigger_queue: queue.Queue, watch_set: WatchSet) -> None:
        self.trigger_queue = snapshot_trigger_queue
        self.watch_set = watch_set
        self._sched_lock = threading.Lock()
        self._scheduled: list = []  # heapq of (due_at_ms, asset_id, event_meta)

    def start(self) -> None:
        threading.Thread(target=self._intake_loop, daemon=True, name="book_snap_intake").start()
        threading.Thread(target=self._scheduler_loop, daemon=True, name="book_snap_sched").start()
        threading.Thread(target=self._tick_loop, daemon=True, name="book_snap_tick").start()
        logger.info("BookSnapshotPoller started (intake + scheduler + tick)")

    def _intake_loop(self) -> None:
        while True:
            try:
                payload = self.trigger_queue.get(timeout=10)
            except queue.Empty:
                continue
            try:
                ev_received = payload.get("event_received_ts_ms")
                asset_id = payload.get("asset_id")
                if not asset_id or ev_received is None:
                    continue
                self.watch_set.add(asset_id, source="chain", state="active")
                with self._sched_lock:
                    for off in _EVENT_OFFSETS_MS:
                        meta = dict(payload)
                        meta["offset_ms"] = off
                        heapq.heappush(self._scheduled, (ev_received + off, asset_id, meta))
            except Exception as e:
                logger.exception(f"book_snap intake failed: {e}")

    def _scheduler_loop(self) -> None:
        while True:
            now_ms = int(time.time() * 1000)
            task = None
            with self._sched_lock:
                if self._scheduled and self._scheduled[0][0] <= now_ms:
                    task = heapq.heappop(self._scheduled)
            if task is None:
                time.sleep(0.05)
                continue
            _, asset_id, meta = task
            try:
                self._do_snapshot(asset_id, "bs_event", meta)
            except Exception as e:
                logger.exception(f"book_snap event snapshot failed: {e}")

    def _tick_loop(self) -> None:
        while True:
            t0 = time.time()
            tokens = self.watch_set.tokens()
            for asset_id in tokens:
                try:
                    self._do_snapshot(asset_id, "tick", None)
                except Exception as e:
                    logger.warning(f"book_snap tick failed for {asset_id[:12]}: {e}")
            elapsed = time.time() - t0
            time.sleep(max(0.5, _TICK_INTERVAL_S - elapsed))

    def _do_snapshot(self, asset_id: str, trigger: str, event_meta: Optional[dict]) -> None:
        book, lat, method = _fetch_book(asset_id)
        row = _build_row(asset_id, book, lat, method, trigger, event_meta)
        _write_row(row)

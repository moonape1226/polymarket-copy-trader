"""
Token metadata cache for Polymarket V2.

Maps tokenId → {market(condition_id), title, outcome, neg_risk}.
Source: gamma-api /markets?clob_token_ids=<tokenId>.
Persistence: /data/book_logger/token_meta.json (atomic write on every new entry).

Used by events_logger (Phase 1), book_snapshot (Phase 2), trade_tape_poller (Phase 3).
Lazy fetch on miss. On gamma-api failure, returns None — callers write null
metadata fields rather than skip the row.

Spec: docs/book_logger_spec.md §5.7.
"""

import json
import logging
import os
import tempfile
import threading
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("BOOK_LOGGER_DATA_DIR", "/data/book_logger")
_PERSIST_PATH = os.path.join(_DATA_DIR, "token_meta.json")
_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
_FETCH_TIMEOUT = 8


_MISS_RETRY_S = 600  # 10 min — review #5: don't permanently cache negative misses


class _TokenMetaCache:
    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        # asset_id → last_attempt_unix_ts; older than _MISS_RETRY_S → retry
        self._misses: Dict[str, float] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not os.path.exists(_PERSIST_PATH):
            return
        try:
            with open(_PERSIST_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache.update(data)
                logger.info(f"token_meta: loaded {len(self._cache)} entries from {_PERSIST_PATH}")
        except Exception as e:
            logger.warning(f"token_meta: load failed: {e}")

    def _persist_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(_PERSIST_PATH), exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=os.path.dirname(_PERSIST_PATH), delete=False, suffix=".tmp"
            ) as tf:
                json.dump(self._cache, tf, separators=(",", ":"))
                tmp_name = tf.name
            os.replace(tmp_name, _PERSIST_PATH)
        except Exception as e:
            logger.warning(f"token_meta: persist failed: {e}")

    def get(self, asset_id: str) -> Optional[Dict[str, Any]]:
        if not asset_id:
            return None
        import time as _time
        now = _time.time()
        with self._lock:
            cached = self._cache.get(asset_id)
            if cached is not None:
                return cached
            miss_ts = self._misses.get(asset_id)
            if miss_ts is not None and (now - miss_ts) < _MISS_RETRY_S:
                return None  # too recent to retry
        try:
            r = requests.get(_GAMMA_URL, params={"clob_token_ids": asset_id}, timeout=_FETCH_TIMEOUT)
            if not r.ok:
                with self._lock:
                    self._misses[asset_id] = now
                return None
            data = r.json()
            if not isinstance(data, list) or not data:
                with self._lock:
                    self._misses[asset_id] = now
                return None
            m = data[0]
            outcomes_raw = m.get("outcomes")
            token_ids_raw = m.get("clobTokenIds")
            if not outcomes_raw or not token_ids_raw:
                return None
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
            try:
                idx = token_ids.index(asset_id)
            except ValueError:
                return None
            entry = {
                "market": m.get("conditionId"),
                "title": m.get("question"),
                "outcome": outcomes[idx] if idx < len(outcomes) else None,
                "neg_risk": bool(m.get("negRisk", False)),
            }
            with self._lock:
                self._cache[asset_id] = entry
                self._persist_locked()
            return entry
        except Exception as e:
            logger.debug(f"token_meta fetch failed for {asset_id[:12]}: {e}")
            return None


META = _TokenMetaCache()

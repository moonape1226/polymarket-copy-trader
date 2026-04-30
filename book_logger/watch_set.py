"""
WatchSet — shared registry of tokens currently being recorded.

Membership rules (spec §5.2):
  - Token added when chain_subscriber sees BS OrderFilled on it
  - Token added when periodic /positions poll shows BS holds it (state=active)
  - Token kept while BS holds it (state=active)
  - Token kept 24h past last BS activity (state=grace)
  - Hard cap 50; eviction priority: grace first, then oldest active

Persistence: data/book_logger/watchset.json (atomic write on every change).
On bot restart, entries with last_touch_ms older than 24h are dropped.

Spec: docs/book_logger_spec.md §5.2.
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("BOOK_LOGGER_DATA_DIR", "/data/book_logger")
_WATCHSET_PATH = os.path.join(_DATA_DIR, "watchset.json")
_HARD_CAP = 50
_GRACE_MS = 24 * 3600 * 1000


class WatchSet:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # token_id → {state, last_touch_ms, source}
        self._tokens: Dict[str, dict] = {}
        self._dropped_active = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(_WATCHSET_PATH):
            return
        try:
            with open(_WATCHSET_PATH) as f:
                data = json.load(f)
            cutoff = int(time.time() * 1000) - _GRACE_MS
            kept = {k: v for k, v in data.items() if int(v.get("last_touch_ms", 0)) > cutoff}
            self._tokens = kept
            logger.info(f"WatchSet loaded {len(kept)} entries (dropped {len(data) - len(kept)} expired)")
        except Exception as e:
            logger.warning(f"WatchSet load failed: {e}")

    def _persist_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(_WATCHSET_PATH), exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=os.path.dirname(_WATCHSET_PATH), delete=False, suffix=".tmp"
            ) as tf:
                json.dump(self._tokens, tf, separators=(",", ":"))
                tmp_name = tf.name
            os.replace(tmp_name, _WATCHSET_PATH)
        except Exception as e:
            logger.warning(f"WatchSet persist failed: {e}")

    def _evict_one_locked(self) -> bool:
        """Eviction priority (review #2):
        1. state=='grace'        — oldest first (any source)
        2. state=='active' and source=='weather_promote'
                                  — oldest first; weather samples never displace BS
        3. state=='active' and source=='chain'
                                  — last resort; bumps `_dropped_active` alert counter
        """
        # Tier 1: grace tokens
        graces = [(k, int(v["last_touch_ms"])) for k, v in self._tokens.items()
                  if v.get("state") == "grace"]
        if graces:
            oldest = min(graces, key=lambda x: x[1])
            del self._tokens[oldest[0]]
            return True
        # Tier 2: active weather-promoted tokens
        wp = [(k, int(v["last_touch_ms"])) for k, v in self._tokens.items()
              if v.get("state") == "active" and v.get("source") == "weather_promote"]
        if wp:
            oldest = min(wp, key=lambda x: x[1])
            del self._tokens[oldest[0]]
            return True
        # Tier 3: any other active (effectively BS-sourced) — last resort, alert
        actives = [(k, int(v["last_touch_ms"])) for k, v in self._tokens.items()]
        if actives:
            oldest = min(actives, key=lambda x: x[1])
            entry = self._tokens.pop(oldest[0])
            self._dropped_active += 1
            logger.warning(
                f"WatchSet evicted BS-source token {oldest[0][:12]}… "
                f"(source={entry.get('source')}, no grace/weather tokens left; "
                f"cap={_HARD_CAP}, total dropped active={self._dropped_active})"
            )
            return True
        return False

    def add(self, token_id: str, source: str, state: str = "active") -> None:
        if not token_id:
            return
        now_ms = int(time.time() * 1000)
        with self._lock:
            existing = self._tokens.get(token_id)
            if existing:
                existing["last_touch_ms"] = now_ms
                existing["state"] = state
                existing["source"] = source
            else:
                while len(self._tokens) >= _HARD_CAP:
                    if not self._evict_one_locked():
                        break
                self._tokens[token_id] = {
                    "state": state,
                    "last_touch_ms": now_ms,
                    "source": source,
                }
            self._persist_locked()

    def mark_grace(self, token_id: str) -> None:
        with self._lock:
            entry = self._tokens.get(token_id)
            if entry:
                entry["state"] = "grace"
                self._persist_locked()

    def expire_grace(self) -> int:
        """Drop grace tokens older than _GRACE_MS. Returns count dropped."""
        cutoff = int(time.time() * 1000) - _GRACE_MS
        with self._lock:
            before = len(self._tokens)
            self._tokens = {
                k: v for k, v in self._tokens.items()
                if not (v.get("state") == "grace" and int(v.get("last_touch_ms", 0)) < cutoff)
            }
            dropped = before - len(self._tokens)
            if dropped:
                self._persist_locked()
        return dropped

    def tokens(self) -> List[str]:
        with self._lock:
            return list(self._tokens.keys())

    def has(self, token_id: str) -> bool:
        with self._lock:
            return token_id in self._tokens

    def state_of(self, token_id: str) -> Optional[str]:
        with self._lock:
            entry = self._tokens.get(token_id)
            return entry.get("state") if entry else None

    def size(self) -> int:
        with self._lock:
            return len(self._tokens)

    def dropped_active_count(self) -> int:
        return self._dropped_active

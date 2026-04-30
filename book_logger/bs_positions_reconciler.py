"""
BS positions reconciler — Phase 2 helper.

Periodic poll of data-api `/positions?user=<BS>` to:
  1. Add tokens BS currently holds to WatchSet (state=active, source="chain")
  2. Mark tokens BS no longer holds as state="grace" (24h grace before drop)
  3. Expire grace entries older than 24h (`watch_set.expire_grace()`)

Without this thread, mark_grace / expire_grace never fire and WatchSet
accumulates active entries indefinitely, eventually evicting BS-touched
tokens incorrectly (review #3).

Spec: docs/book_logger_spec.md §5.2.
"""

import logging
import threading
import time
from typing import Iterable

import requests

from watch_set import WatchSet

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 300  # 5 min per spec §5.2
_DATA_API = "https://data-api.polymarket.com/positions"
_FETCH_TIMEOUT = 15


def _fetch_bs_holdings(wallet: str) -> set:
    """Return set of asset_id strings BS currently holds (size > 0)."""
    try:
        r = requests.get(
            _DATA_API,
            params={"user": wallet, "sizeThreshold": "0.01"},
            timeout=_FETCH_TIMEOUT,
        )
        if not r.ok:
            logger.warning(f"reconciler: /positions {r.status_code} for {wallet[:8]}")
            return set()
        data = r.json()
        if not isinstance(data, list):
            return set()
        out = set()
        for p in data:
            if not isinstance(p, dict):
                continue
            aid = p.get("asset")
            try:
                size = float(p.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
            if aid and size > 0:
                out.add(str(aid))
        return out
    except Exception as e:
        logger.warning(f"reconciler: /positions fetch failed for {wallet[:8]}: {e}")
        return set()


def _reconcile_once(watch_set: WatchSet, wallets: Iterable[str]) -> None:
    bs_holdings: set = set()
    for w in wallets:
        bs_holdings |= _fetch_bs_holdings(w)

    # 1. Make sure every BS-held token is in WatchSet as active
    for aid in bs_holdings:
        watch_set.add(aid, source="chain", state="active")

    # 2. Mark chain-source tokens BS no longer holds as grace
    transitioned = 0
    for aid in watch_set.tokens():
        # Only move chain-sourced tokens to grace; weather promotions follow
        # their own lifecycle in weather_universe (re-promotion or eviction).
        # Read state via watch_set state_of, source check via internal items().
        with watch_set._lock:  # noqa: SLF001 — minimal accessor; OK in same package
            entry = watch_set._tokens.get(aid)
            if not entry:
                continue
            if entry.get("source") != "chain":
                continue
            if entry.get("state") != "active":
                continue
            if aid in bs_holdings:
                continue
        watch_set.mark_grace(aid)
        transitioned += 1

    # 3. Drop grace entries older than 24h
    expired = watch_set.expire_grace()

    logger.info(
        f"reconciler: bs_holdings={len(bs_holdings)} "
        f"watchset_size={watch_set.size()} "
        f"→grace={transitioned} expired_grace={expired}"
    )


def run_loop(watch_set: WatchSet, wallets: Iterable[str]) -> None:
    wallets = list(wallets)
    logger.info(f"bs_positions_reconciler started: poll={_POLL_INTERVAL_S}s wallets={len(wallets)}")
    # Run once immediately so WatchSet is seeded with existing BS holdings.
    while True:
        try:
            _reconcile_once(watch_set, wallets)
        except Exception as e:
            logger.exception(f"reconciler loop error: {e}")
        time.sleep(_POLL_INTERVAL_S)


def start(watch_set: WatchSet, wallets: Iterable[str]) -> None:
    threading.Thread(
        target=run_loop, args=(watch_set, list(wallets)),
        daemon=True, name="bs_positions_reconciler",
    ).start()

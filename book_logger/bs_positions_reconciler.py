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


def _fetch_bs_holdings(wallet: str) -> set | None:
    """Return set of asset_id strings BS currently holds (size > 0).

    Returns None (not an empty set) on any fetch/parse failure so the caller
    can tell "BS holds nothing" apart from "we don't know" and avoid expiring
    still-held tokens during an API outage."""
    try:
        r = requests.get(
            _DATA_API,
            params={"user": wallet, "sizeThreshold": "0.01"},
            timeout=_FETCH_TIMEOUT,
        )
        if not r.ok:
            logger.warning(f"reconciler: /positions {r.status_code} for {wallet[:8]}")
            return None
        data = r.json()
        if not isinstance(data, list):
            logger.warning(f"reconciler: /positions non-list for {wallet[:8]}")
            return None
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
        return None


def _reconcile_once(watch_set: WatchSet, wallets: Iterable[str]) -> None:
    bs_holdings: set = set()
    fetch_ok = True
    # Snapshot time BEFORE fetching holdings: a token touched after this is
    # newer than our holdings view, so the grace decision based on this
    # snapshot must not clobber it (D3).
    cutoff_ms = int(time.time() * 1000)
    for w in wallets:
        held = _fetch_bs_holdings(w)
        if held is None:
            fetch_ok = False
            continue
        bs_holdings |= held

    # 1. Make sure every BS-held token is in WatchSet as active (safe even on
    #    partial data — adding never loses tokens)
    for aid in bs_holdings:
        watch_set.add(aid, source="chain", state="active")

    # 2. Mark chain-source tokens BS no longer holds as grace.
    #    Skip entirely if any wallet fetch failed: an incomplete holdings view
    #    would otherwise grace-expire tokens BS still holds (P2).
    transitioned = 0
    if not fetch_ok:
        # Skip BOTH grace transitions and grace expiration: with an incomplete
        # holdings view we must not drop tokens that may still be held (A8).
        logger.warning(
            f"reconciler: holdings fetch incomplete — skipping grace "
            f"transitions AND expiration this cycle "
            f"(watchset_size={watch_set.size()})"
        )
        return
    for aid in watch_set.tokens():
        # Only chain-sourced active tokens BS no longer holds. The conditional
        # transition re-validates source/state/staleness atomically under the
        # lock, so enumerating here is race-safe and needs no _tokens peek (D3).
        if aid in bs_holdings:
            continue
        if watch_set.mark_grace_if_current(aid, "chain", "active", cutoff_ms):
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

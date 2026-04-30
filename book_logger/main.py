"""
book-logger — standalone Polymarket order-book / event recorder.

Phase 1: subscribe to V2 OrderFilled events for tracked wallets, enrich each
event in a worker thread, write JSONL rows to /data/book_logger/bs_events/.

Reads /app/config.json (mounted read-only) for `wallets_to_track`.

Independent of the copy-trader. A crash here cannot affect copy execution;
the bot has its own ChainFeed in src/chain_feed.py.

Spec: docs/book_logger_spec.md.
"""

import json
import logging
import os
import queue
import sys
import threading
import time

from chain_subscriber import ChainSubscriber
from events_logger import worker_loop
from watch_set import WatchSet
from book_snapshot import BookSnapshotPoller
import weather_universe
import bs_positions_reconciler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("BOOK_LOGGER_CONFIG", "/app/config.json")


def _load_wallets() -> list:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.error(f"config not found at {CONFIG_PATH}")
        sys.exit(1)
    wallets = [w.lower() for w in (cfg.get("wallets_to_track") or [])]
    return wallets


_events_dropped = 0
_snap_dropped = 0
_dropped_lock = threading.Lock()


def _on_events_drop() -> None:
    global _events_dropped
    with _dropped_lock:
        _events_dropped += 1


def _on_snap_drop() -> None:
    global _snap_dropped
    with _dropped_lock:
        _snap_dropped += 1


def _drop_reporter(watch_set: WatchSet) -> None:
    last_e = last_s = 0
    while True:
        time.sleep(60)
        with _dropped_lock:
            cur_e, cur_s = _events_dropped, _snap_dropped
        if cur_e != last_e or cur_s != last_s:
            logger.warning(
                f"drops: events_logger={cur_e} snapshot_trigger={cur_s} "
                f"watchset_size={watch_set.size()}"
            )
            last_e, last_s = cur_e, cur_s


def main() -> None:
    wallets = _load_wallets()
    if not wallets:
        logger.error("config has no wallets_to_track; nothing to do")
        sys.exit(1)
    logger.info(f"book-logger Phase 1 starting; watching {len(wallets)} wallet(s)")
    for w in wallets:
        logger.info(f"  watch: {w}")

    events_queue: "queue.Queue" = queue.Queue(maxsize=10000)
    snapshot_queue: "queue.Queue" = queue.Queue(maxsize=10000)
    bs_addresses = {w.lower() for w in wallets}

    # Phase 2: shared WatchSet (used by snapshot poller + weather universe)
    watch_set = WatchSet()

    # Phase 1 worker: consumes events_queue, writes bs_events.jsonl
    threading.Thread(
        target=worker_loop, args=(events_queue, bs_addresses),
        daemon=True, name="events_logger",
    ).start()

    # Phase 2: book_snapshot poller (3 internal threads)
    BookSnapshotPoller(snapshot_queue, watch_set).start()

    # Phase 2W: weather-universe collector (also seeds WatchSet via promotion)
    weather_universe.start(watch_set)

    # /positions reconciler: keeps BS-held tokens in WatchSet active and moves
    # exited positions to grace (review #3). Without this, mark_grace and
    # expire_grace are never called and WatchSet accumulates indefinitely.
    bs_positions_reconciler.start(watch_set, wallets)

    threading.Thread(
        target=_drop_reporter, args=(watch_set,),
        daemon=True, name="drop_reporter",
    ).start()

    # ChainSubscriber pushes raw log to events_queue + decoded payload to snapshot_queue
    subscriber = ChainSubscriber(
        wallets, events_queue, _on_events_drop,
        snapshot_queue=snapshot_queue, snapshot_dropped_callback=_on_snap_drop,
    )
    subscriber.start()

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()

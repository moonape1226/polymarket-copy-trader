#!/usr/bin/env python3
"""
Polymarket Trade Logger - multi-wallet version
Polls each wallet in wallets_to_observe and writes a separate CSV per wallet.
"""

import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Set

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
CONFIG_PATH   = os.getenv("CONFIG_PATH", "/app/config.json")
DATA_DIR      = os.getenv("DATA_DIR", "/data")
DATA_API      = "https://data-api.polymarket.com"

CSV_FIELDS = [
    "trade_timestamp", "detected_at", "wallet_name", "wallet_address",
    "title", "outcome", "side", "price", "size", "usd_value",
    "condition_id", "asset_id", "tx_hash",
]


def load_wallets() -> Dict[str, str]:
    """Return {name: address} from wallets_to_observe in config."""
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    wallets = config.get("wallets_to_observe", {})
    if not wallets:
        raise ValueError("No wallets_to_observe in config.json")
    return wallets


def csv_path(name: str) -> str:
    safe = name.replace(" ", "_").replace(".", "_")
    return os.path.join(DATA_DIR, f"observe_{safe}.csv")


def init_csv(name: str):
    path = csv_path(name)
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
        logger.info(f"Created {path}")


def fetch_activity(address: str, limit: int = 100):
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": address, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"[{address[:8]}] fetch failed: {e}")
        return None


def log_trade(name: str, address: str, record: dict, detected_at: str):
    trade_ts  = datetime.fromtimestamp(int(record["timestamp"]), tz=timezone.utc).isoformat()
    rec_type  = record.get("type", "TRADE")
    size      = float(record.get("size", 0))
    usd_value = float(record.get("usdcSize", 0))

    if rec_type == "REDEEM":
        side    = "REDEEM"
        price   = (usd_value / size) if size else 0.0
        title   = ""
        outcome = ""
    else:
        side    = record.get("side", "")
        price   = float(record.get("price", 0))
        title   = record.get("title", "")
        outcome = record.get("outcome", "")

    with open(csv_path(name), "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow({
            "trade_timestamp":  trade_ts,
            "detected_at":      detected_at,
            "wallet_name":      name,
            "wallet_address":   address,
            "title":            title,
            "outcome":          outcome,
            "side":             side,
            "price":            f"{price:.6f}",
            "size":             f"{size:.2f}",
            "usd_value":        f"{usd_value:.2f}",
            "condition_id":     record.get("conditionId", ""),
            "asset_id":         record.get("asset", ""),
            "tx_hash":          record.get("transactionHash", ""),
        })

    if rec_type == "REDEEM":
        logger.info(f"[{name}] REDEEM  ${usd_value:>8.2f}  conditionId={record.get('conditionId','')[:16]}…")
    else:
        logger.info(f"[{name}] {side:4s}  ${usd_value:>8.2f}  '{title} ({outcome})'  @ {price:.4f}")


def main():
    wallets = load_wallets()
    logger.info(f"Observing {len(wallets)} wallets: {list(wallets.keys())}")
    logger.info(f"Output dir: {DATA_DIR}  Poll interval: {POLL_INTERVAL}s")

    for name in wallets:
        init_csv(name)

    # Per-wallet state
    last_ts:    Dict[str, int]      = {name: int(time.time()) - 60 for name in wallets}
    seen_hashes: Dict[str, Set[str]] = {name: set() for name in wallets}

    while True:
        detected_at = datetime.now(timezone.utc).isoformat()

        for name, address in wallets.items():
            records = fetch_activity(address, limit=100)
            if not records:
                continue

            new_records = [
                r for r in records
                if r.get("type") in ("TRADE", "REDEEM")
                and int(r.get("timestamp", 0)) > last_ts[name]
                and r.get("transactionHash") not in seen_hashes[name]
            ]

            new_records.sort(key=lambda r: int(r.get("timestamp", 0)))

            for record in new_records:
                log_trade(name, address, record, detected_at)
                seen_hashes[name].add(record.get("transactionHash", ""))

            if new_records:
                last_ts[name] = max(int(r.get("timestamp", 0)) for r in new_records)

            if len(seen_hashes[name]) > 500:
                seen_hashes[name].clear()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

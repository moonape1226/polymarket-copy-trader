"""
BS chain events worker — consumes the chain subscriber queue and writes
data/bs_events/YYYY-MM-DD.jsonl with full enrichment.

Enrichment per row:
  log_index/tx_hash normalization → order_hash (topics[1])
  → role/wallet_side from maker/taker membership
  → fee_amount from data[256:320]
  → block_ts via eth_getBlockByNumber (LRU-cached per block)
  → metadata via token_meta.META.get
  → tx_companions via eth_getTransactionReceipt (cached per tx_hash)

Spec: docs/book_logger_spec.md §4.2 + §6 Phase 1.
"""

import datetime
import json
import logging
import os
import queue
import threading
from typing import Any, Dict, List, Optional

import requests

from token_meta import META

logger = logging.getLogger(__name__)

_RPC_URLS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
)
_RPC_TIMEOUT = 10
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"

_DATA_DIR = os.environ.get("BOOK_LOGGER_DATA_DIR", "/data/book_logger")
_BS_EVENTS_DIR = os.path.join(_DATA_DIR, "bs_events")

_block_ts_cache: Dict[int, int] = {}
_block_ts_lock = threading.Lock()
_receipt_cache: Dict[str, Optional[Dict[str, Any]]] = {}
_receipt_lock = threading.Lock()


def _rpc(method: str, params: list, timeout: int = _RPC_TIMEOUT) -> Optional[Any]:
    for url in _RPC_URLS:
        try:
            r = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=timeout,
            )
            if r.ok:
                data = r.json()
                if "result" in data:
                    return data["result"]
        except Exception:
            continue
    return None


def _block_ts_ms(block: int) -> Optional[int]:
    with _block_ts_lock:
        cached = _block_ts_cache.get(block)
    if cached is not None:
        return cached
    res = _rpc("eth_getBlockByNumber", [hex(block), False])
    if not res or "timestamp" not in res:
        return None
    ts_ms = int(res["timestamp"], 16) * 1000
    with _block_ts_lock:
        _block_ts_cache[block] = ts_ms
    return ts_ms


def _receipt(tx_hash: str) -> Optional[Dict[str, Any]]:
    with _receipt_lock:
        if tx_hash in _receipt_cache:
            return _receipt_cache[tx_hash]
    res = _rpc("eth_getTransactionReceipt", [tx_hash])
    with _receipt_lock:
        _receipt_cache[tx_hash] = res
    return res


def _decode_log(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    topics = raw.get("topics") or []
    if len(topics) < 4 or topics[0].lower() != ORDER_FILLED_TOPIC.lower():
        return None
    data_hex = raw.get("data", "")
    if data_hex.startswith("0x"):
        data_hex = data_hex[2:]
    if len(data_hex) < 64 * 7:
        return None
    try:
        order_hash = topics[1].lower()
        maker = "0x" + topics[2][-40:].lower()
        taker = "0x" + topics[3][-40:].lower()
        side_enum = int(data_hex[0:64], 16)
        token_id_int = int(data_hex[64:128], 16)
        maker_amt = int(data_hex[128:192], 16)
        taker_amt = int(data_hex[192:256], 16)
        fee_raw = int(data_hex[256:320], 16)
    except (ValueError, IndexError):
        return None
    if side_enum not in (0, 1) or token_id_int == 0:
        return None
    if side_enum == 0:
        shares_raw, usdc_raw = taker_amt, maker_amt
    else:
        shares_raw, usdc_raw = maker_amt, taker_amt
    if shares_raw == 0:
        return None
    return {
        "order_hash": order_hash,
        "maker": maker,
        "taker": taker,
        "order_side_enum": side_enum,
        "order_side": "BUY" if side_enum == 0 else "SELL",
        "token_id": str(token_id_int),
        "shares": shares_raw / 1e6,
        "usdc": usdc_raw / 1e6,
        "price": (usdc_raw / shares_raw) if shares_raw else 0.0,
        "fee_amount_raw": str(fee_raw),
        "fee_amount_num": fee_raw / 1e6,
    }


def _decode_companions(receipt: Optional[Dict[str, Any]], own_log_index: int) -> Optional[List[Dict[str, Any]]]:
    if not receipt or "logs" not in receipt:
        return None
    out: List[Dict[str, Any]] = []
    for log in receipt["logs"]:
        try:
            li_raw = log.get("logIndex", "")
            if not li_raw:
                continue
            li = int(li_raw, 16) if isinstance(li_raw, str) and li_raw.startswith("0x") else int(li_raw)
            if li == own_log_index:
                continue
            decoded = _decode_log(log)
            if not decoded:
                continue
            tx_hash = (log.get("transactionHash") or "").lower()
            out.append({
                "event_id": f"{tx_hash}:{li}",
                "order_hash": decoded["order_hash"],
                "asset_id": decoded["token_id"],
                "order_side": decoded["order_side"],
                "size": decoded["shares"],
                "price": decoded["price"],
                "role": "no-BS",
            })
        except Exception:
            continue
    return out


_write_lock = threading.Lock()


def _output_path(ts_ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
    return os.path.join(_BS_EVENTS_DIR, dt.strftime("%Y-%m-%d.jsonl"))


def _write_row(row: Dict[str, Any]) -> None:
    path = _output_path(row["ts_ms"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with _write_lock:
        with open(path, "a") as f:
            f.write(line)


def _process_one(item: Dict[str, Any], bs_addresses: set) -> None:
    raw = item.get("raw_log") or {}
    received_ts_ms = int(item.get("received_ts", 0) * 1000)

    decoded = _decode_log(raw)
    if not decoded:
        return

    tx_hash = (raw.get("transactionHash") or "").lower()
    li_raw = raw.get("logIndex", "")
    log_index = (
        int(li_raw, 16) if isinstance(li_raw, str) and li_raw.startswith("0x")
        else int(li_raw or 0)
    )
    block_hex = raw.get("blockNumber", "0x0")
    block = (
        int(block_hex, 16) if isinstance(block_hex, str) and block_hex.startswith("0x")
        else int(block_hex or 0)
    )
    event_id = f"{tx_hash}:{log_index}"

    if decoded["maker"] in bs_addresses:
        role, wallet = "MAKER", decoded["maker"]
    elif decoded["taker"] in bs_addresses:
        role, wallet = "TAKER", decoded["taker"]
    else:
        return

    if role == "MAKER":
        wallet_side = decoded["order_side"]
    else:
        wallet_side = "SELL" if decoded["order_side"] == "BUY" else "BUY"

    block_ts_ms = _block_ts_ms(block)
    meta = META.get(decoded["token_id"]) or {}
    receipt = _receipt(tx_hash)
    tx_companions = _decode_companions(receipt, log_index) if receipt else None

    ts_ms = block_ts_ms if block_ts_ms is not None else received_ts_ms
    ts_iso = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")

    row = {
        "event_id": event_id,
        "order_hash": decoded["order_hash"],
        "ts": ts_iso,
        "ts_ms": ts_ms,
        "block_ts_ms": block_ts_ms,
        "received_ts_ms": received_ts_ms,
        "tx_hash": tx_hash,
        "block": block,
        "log_index": log_index,
        "wallet": wallet,
        "asset_id": decoded["token_id"],
        "market": meta.get("market"),
        "title": meta.get("title"),
        "outcome": meta.get("outcome"),
        "neg_risk": meta.get("neg_risk"),
        "order_side": decoded["order_side"],
        "wallet_side": wallet_side,
        "size": decoded["shares"],
        "price": decoded["price"],
        "role": role,
        "fee_amount_raw": decoded["fee_amount_raw"],
        "fee_amount_num": decoded["fee_amount_num"],
        "tx_companions": tx_companions,
    }
    _write_row(row)
    logger.info(
        f"bs_events: {role} {wallet_side} {decoded['shares']:.2f}sh "
        f"@ ${decoded['price']:.4f} asset={decoded['token_id'][:12]}… "
        f"event={event_id[:18]}…"
    )


def worker_loop(in_queue: queue.Queue, bs_addresses: set) -> None:
    logger.info(f"events_logger worker started, BS addresses: {len(bs_addresses)}")
    while True:
        try:
            item = in_queue.get(timeout=10)
        except queue.Empty:
            continue
        try:
            _process_one(item, bs_addresses)
        except Exception as e:
            logger.exception(f"events_logger: process_one failed: {e}")

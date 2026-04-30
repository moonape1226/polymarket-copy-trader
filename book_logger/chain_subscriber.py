"""
Chain WS subscriber for V2 OrderFilled events.

Independent of the bot's src/chain_feed.py — this service has its own WS
connection so a crash here cannot affect copy-trade execution.

Subscribes to V2 OrderFilled on both standard + neg-risk exchanges, filtered
by tracked wallets (BS) appearing as maker or taker. Emits raw RPC log dicts
to a queue consumed by events_logger.

Two providers run concurrently with dedup; one provider's stream is sufficient.

Spec: docs/book_logger_spec.md §6 Phase 1.
"""

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from queue import Queue, Full
from typing import List, Optional, Tuple

import requests
import websockets

logger = logging.getLogger(__name__)

RPC_ENDPOINTS: List[Tuple[str, str]] = [
    ("wss://polygon-bor-rpc.publicnode.com", "https://polygon-bor-rpc.publicnode.com"),
    ("wss://polygon.drpc.org", "https://polygon.drpc.org"),
]

PING_INTERVAL = 10
PING_TIMEOUT = 10

DEDUP_MAX = 50000
DEDUP_TTL = 600

BACKFILL_MAX_BLOCKS = 2000

ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
ORDER_FILLED_TOPIC_LC = ORDER_FILLED_TOPIC.lower()

# V2 CTF Exchange contracts on Polygon (post-2026-04-28 cutover)
CTF_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
EXCHANGES = [CTF_EXCHANGE, NEG_RISK_EXCHANGE]


def _pad_addr_topic(addr: str) -> str:
    return "0x" + "00" * 12 + addr.lower().replace("0x", "")


class ChainSubscriber:
    """Subscribe to V2 OrderFilled, push raw log dicts to `out_queue`.

    `out_queue` items have shape `{"raw_log": <log dict>, "received_ts": <unix s>}`.
    Queue is bounded; full pushes call `dropped_callback()` and never block.
    """

    def __init__(self, wallets: list, out_queue: Queue, dropped_callback,
                 snapshot_queue: Optional[Queue] = None,
                 snapshot_dropped_callback=None):
        self.wallets = {w.lower() for w in wallets}
        self.out_queue = out_queue
        self.snapshot_queue = snapshot_queue
        self._dropped = dropped_callback
        self._snap_dropped = snapshot_dropped_callback or (lambda: None)
        self._thread: Optional[threading.Thread] = None
        self._seen_logs: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self._last_block: int = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="chain_subscriber")
        self._thread.start()
        logger.info(
            f"chain_subscriber started: {len(self.wallets)} wallet(s) on "
            f"{len(RPC_ENDPOINTS)} provider(s)"
        )

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._main())

    async def _main(self) -> None:
        await asyncio.gather(*(
            self._ws_loop(ws_url, http_url) for ws_url, http_url in RPC_ENDPOINTS
        ))

    async def _ws_loop(self, ws_url: str, http_url: str) -> None:
        tag = ws_url.split("//", 1)[-1].split("/", 1)[0]
        backoff = 5
        first_connect = True
        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=PING_TIMEOUT,
                ) as ws:
                    backoff = 5
                    sub_id = 0
                    for wallet in self.wallets:
                        wtopic = _pad_addr_topic(wallet)
                        sub_id += 1
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": sub_id, "method": "eth_subscribe",
                            "params": ["logs", {
                                "address": EXCHANGES,
                                "topics": [ORDER_FILLED_TOPIC, None, wtopic],
                            }],
                        }))
                        sub_id += 1
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": sub_id, "method": "eth_subscribe",
                            "params": ["logs", {
                                "address": EXCHANGES,
                                "topics": [ORDER_FILLED_TOPIC, None, None, wtopic],
                            }],
                        }))
                    logger.info(
                        f"chain_subscriber [{tag}]: subscribed for "
                        f"{len(self.wallets)} wallet(s) on {len(EXCHANGES)} exchanges"
                    )

                    if first_connect:
                        first_connect = False
                        await self._anchor_last_block(http_url, tag)
                    else:
                        await self._backfill_http(http_url, tag)

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if "id" in msg:
                            continue
                        params = msg.get("params", {})
                        log = params.get("result")
                        if isinstance(log, dict):
                            self._handle_log(log, source=tag)
            except Exception as e:
                logger.warning(
                    f"chain_subscriber [{tag}] disconnected: {e}, reconnecting in {backoff}s"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _anchor_last_block(self, http_url: str, tag: str) -> None:
        if self._last_block > 0:
            return
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: requests.post(
                http_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                timeout=10,
            ))
            head = int(resp.json()["result"], 16)
            self._last_block = head
            logger.info(f"chain_subscriber [{tag}]: anchored last_block={head}")
        except Exception as e:
            logger.warning(f"chain_subscriber [{tag}]: anchor failed: {e}")

    async def _backfill_http(self, http_url: str, tag: str) -> None:
        if self._last_block <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
            head_resp = await loop.run_in_executor(None, lambda: requests.post(
                http_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                timeout=10,
            ))
            head = int(head_resp.json()["result"], 16)
            # Review #4: rewind 5 blocks for overlap; (tx_hash, log_index)
            # dedup handles repeats. Without overlap, logs in `_last_block`'s
            # block emitted after the last live event were lost on reconnect.
            from_block = max(0, self._last_block - 5)
            if from_block > head:
                return
            to_block = min(head, from_block + BACKFILL_MAX_BLOCKS - 1)
            gap = to_block - from_block + 1
            logger.info(
                f"chain_subscriber [{tag}]: backfilling {gap} block(s) "
                f"[{from_block} → {to_block}]"
            )
            total = 0
            for wallet in self.wallets:
                wtopic = _pad_addr_topic(wallet)
                for slot_topics in (
                    [ORDER_FILLED_TOPIC, None, wtopic],
                    [ORDER_FILLED_TOPIC, None, None, wtopic],
                ):
                    params = [{
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                        "address": EXCHANGES,
                        "topics": slot_topics,
                    }]
                    resp = await loop.run_in_executor(None, lambda p=params: requests.post(
                        http_url,
                        json={"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": p},
                        timeout=20,
                    ))
                    result = resp.json().get("result", [])
                    if isinstance(result, list):
                        for log in result:
                            self._handle_log(log, source=f"{tag}-backfill")
                            total += 1
            if total:
                logger.info(f"chain_subscriber [{tag}]: backfill processed {total} log(s)")
            if to_block > self._last_block:
                self._last_block = to_block
        except Exception as e:
            logger.warning(f"chain_subscriber [{tag}]: backfill failed: {e}")

    def _dedup_seen(self, key: Tuple[str, str]) -> bool:
        now = time.time()
        if key in self._seen_logs:
            return True
        self._seen_logs[key] = now
        cutoff = now - DEDUP_TTL
        while self._seen_logs:
            oldest_key, oldest_ts = next(iter(self._seen_logs.items()))
            if oldest_ts < cutoff or len(self._seen_logs) > DEDUP_MAX:
                self._seen_logs.popitem(last=False)
            else:
                break
        return False

    def _handle_log(self, log: dict, source: str = "") -> None:
        topics = log.get("topics") or []
        if len(topics) < 4 or topics[0].lower() != ORDER_FILLED_TOPIC_LC:
            return
        tx_hash = log.get("transactionHash", "") or ""
        log_index = log.get("logIndex", "") or ""
        dedup_key = (tx_hash.lower(), str(log_index).lower())
        if tx_hash and log_index != "" and self._dedup_seen(dedup_key):
            return

        block_hex = log.get("blockNumber", "0x0")
        try:
            block = int(block_hex, 16)
        except (TypeError, ValueError):
            block = 0
        if block > self._last_block:
            self._last_block = block

        # Quick wallet check before pushing — keep filter consistent with the
        # eth_subscribe topic filter so a misbehaving provider can't spam us.
        maker = "0x" + topics[2][-40:].lower()
        taker = "0x" + topics[3][-40:].lower()
        if maker not in self.wallets and taker not in self.wallets:
            return

        received_ts = time.time()
        item = {"raw_log": log, "received_ts": received_ts, "source": source}
        try:
            self.out_queue.put_nowait(item)
        except Full:
            self._dropped()
        except Exception:
            self._dropped()

        # Phase 2: push decoded snapshot trigger payload to book_snapshot.
        # Cheap inline decode — no RPC, no metadata, no receipt fetch (those
        # belong in events_logger). event_block_ts_ms left null (Phase 1 spec).
        if self.snapshot_queue is None:
            return
        try:
            data_hex = log.get("data", "")
            if data_hex.startswith("0x"):
                data_hex = data_hex[2:]
            if len(data_hex) < 64 * 7:
                return
            order_side_enum = int(data_hex[0:64], 16)
            token_id_int = int(data_hex[64:128], 16)
            maker_amt = int(data_hex[128:192], 16)
            taker_amt = int(data_hex[192:256], 16)
            if order_side_enum not in (0, 1) or token_id_int == 0:
                return
            order_side = "BUY" if order_side_enum == 0 else "SELL"
            role = "MAKER" if maker in self.wallets else "TAKER"
            wallet_buys = (role == "MAKER") == (order_side_enum == 0)
            wallet_side = "BUY" if wallet_buys else "SELL"
            if order_side_enum == 0:
                shares_raw, usdc_raw = taker_amt, maker_amt
            else:
                shares_raw, usdc_raw = maker_amt, taker_amt
            shares = shares_raw / 1e6 if shares_raw else 0.0
            price = (usdc_raw / shares_raw) if shares_raw else 0.0
            try:
                log_index_int = int(log_index, 16) if isinstance(log_index, str) and log_index.startswith("0x") else int(log_index)
            except Exception:
                log_index_int = 0
            try:
                block_int = int(block_hex, 16) if isinstance(block_hex, str) else int(block_hex or 0)
            except Exception:
                block_int = 0
            payload = {
                "event_id": f"{tx_hash.lower()}:{log_index_int}",
                "tx_hash": tx_hash.lower(),
                "log_index": log_index_int,
                "block": block_int,
                "asset_id": str(token_id_int),
                "event_received_ts_ms": int(received_ts * 1000),
                "event_block_ts_ms": None,
                "bs_order_side": order_side,
                "bs_wallet_side": wallet_side,
                "bs_role": role,
                "bs_size": shares,
                "bs_price": price,
            }
            self.snapshot_queue.put_nowait(payload)
        except Full:
            self._snap_dropped()
        except Exception:
            self._snap_dropped()

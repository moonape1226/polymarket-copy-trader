"""
Polygon chain WebSocket feed for real-time BS Safe trade detection.

Subscribes to OrderFilled events on the CTF Exchange and Neg Risk CTF Exchange,
filtered by tracked wallets appearing as maker or taker. Emits decoded trade
events via a queue. Latency from on-chain fill to queue: ~1-3s (vs ~30-60s for
the /positions + /activity polling path).

Resilience:
  - Multiple RPC providers subscribed in parallel; events dedup'd by
    (tx_hash, log_index) so either provider's stream is sufficient.
  - Aggressive ping (10s) detects dead sockets faster than the default 20s.
  - On reconnect, eth_getLogs backfills blocks since last seen event so a
    brief WS outage does not drop fills permanently.

Event signature (CTF Exchange V2, post-2026-04-28 cutover):
  OrderFilled(
    bytes32 indexed orderHash, address indexed maker, address indexed taker,
    uint8 side, uint256 tokenId,
    uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee,
    bytes32 builder, bytes32 metadata
  )
  side: 0=BUY (maker pays pUSD, takes shares), 1=SELL (maker gives shares, takes pUSD).

Emitted event shape (on queue):
  {
    "wallet": "0x...",      # tracked wallet that filled
    "asset": "12345...",    # decimal asset id (matches data-api format)
    "side": "buy" | "sell",
    "size": float,          # shares (6 decimals)
    "price": float,         # USDC/share
    "tx_hash": "0x...",
    "block": int,
    "received_ts": float,   # local time we saw the log
  }
"""
import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from queue import Queue, Empty
from typing import Optional

import requests
import websockets

logger = logging.getLogger(__name__)

# (ws_url, http_url) pairs. Multiple providers subscribe in parallel; events
# are dedup'd so a single provider seeing the log is sufficient.
RPC_ENDPOINTS = [
    ("wss://polygon-bor-rpc.publicnode.com", "https://polygon-bor-rpc.publicnode.com"),
    ("wss://polygon.drpc.org",               "https://polygon.drpc.org"),
]

PING_INTERVAL = 10   # send ping every 10s (was 20)
PING_TIMEOUT = 10    # drop connection if no pong in 10s

# Dedup log-id cache (shared across providers). Bounded to avoid unbounded
# growth; OrderedDict gives O(1) LRU eviction.
DEDUP_MAX = 50000
DEDUP_TTL = 600  # seconds; mostly for memory hygiene, collisions impossible

# Cap blocks per backfill call to keep eth_getLogs cheap. Polygon ~2s/block,
# so 2000 blocks ≈ 67min of outage coverage.
BACKFILL_MAX_BLOCKS = 2000

# keccak256("OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)")
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
ORDER_FILLED_TOPIC_LC = ORDER_FILLED_TOPIC.lower()

# Polymarket CTF Exchange V2 contracts on Polygon (cutover 2026-04-28 11:00 UTC).
CTF_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
EXCHANGES = [CTF_EXCHANGE, NEG_RISK_EXCHANGE]


def _pad_addr_topic(addr: str) -> str:
    return "0x" + "00" * 12 + addr.lower().replace("0x", "")


class ChainFeed:
    def __init__(self, wallets: list[str]):
        self.wallets = {w.lower() for w in wallets}
        self.queue: "Queue[dict]" = Queue()
        self._thread: Optional[threading.Thread] = None
        # Shared dedup + last-block state across all provider loops.
        self._seen_logs: "OrderedDict[tuple[str, str], float]" = OrderedDict()
        self._last_block: int = 0  # max block we've successfully processed

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="chain_feed")
        self._thread.start()
        logger.info(
            f"Chain feed started for {len(self.wallets)} wallet(s) "
            f"across {len(RPC_ENDPOINTS)} provider(s)"
        )

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._main())

    async def _main(self):
        # Fan out to all providers concurrently. Each loop independently reconnects.
        await asyncio.gather(*(
            self._ws_loop(ws_url, http_url) for ws_url, http_url in RPC_ENDPOINTS
        ))

    async def _ws_loop(self, ws_url: str, http_url: str):
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
                        # maker=wallet
                        sub_id += 1
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": sub_id, "method": "eth_subscribe",
                            "params": ["logs", {
                                "address": EXCHANGES,
                                "topics": [ORDER_FILLED_TOPIC, None, wtopic],
                            }],
                        }))
                        # taker=wallet
                        sub_id += 1
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": sub_id, "method": "eth_subscribe",
                            "params": ["logs", {
                                "address": EXCHANGES,
                                "topics": [ORDER_FILLED_TOPIC, None, None, wtopic],
                            }],
                        }))
                    logger.info(
                        f"Chain feed [{tag}]: subscribed for {len(self.wallets)} wallet(s) "
                        f"on {len(EXCHANGES)} exchanges"
                    )

                    # On reconnect, pull missed logs via HTTP. First connect just
                    # anchors _last_block to "now" so we don't dump historical data.
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
                            continue  # subscription ack
                        params = msg.get("params", {})
                        log = params.get("result")
                        if isinstance(log, dict):
                            self._handle_log(log, source=tag)
            except Exception as e:
                logger.warning(
                    f"Chain feed [{tag}] disconnected: {e}, reconnecting in {backoff}s"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _anchor_last_block(self, http_url: str, tag: str):
        """On first connect, set _last_block to current head so backfill on
        the next reconnect only covers the actual outage window."""
        if self._last_block > 0:
            return  # another provider already anchored
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: requests.post(
                http_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                timeout=10,
            ))
            head = int(resp.json()["result"], 16)
            self._last_block = head
            logger.info(f"Chain feed [{tag}]: anchored last_block={head}")
        except Exception as e:
            logger.warning(f"Chain feed [{tag}]: anchor failed: {e}")

    async def _backfill_http(self, http_url: str, tag: str):
        """Pull OrderFilled logs from _last_block+1 to head via eth_getLogs.
        Dedup guards against overlap with live WS."""
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
            from_block = self._last_block + 1
            if from_block > head:
                return
            # Cap window so we don't ask for too much.
            to_block = min(head, from_block + BACKFILL_MAX_BLOCKS - 1)
            gap = to_block - from_block + 1
            logger.info(
                f"Chain feed [{tag}]: backfilling {gap} block(s) "
                f"[{from_block} → {to_block}]"
            )
            total = 0
            for wallet in self.wallets:
                wtopic = _pad_addr_topic(wallet)
                for slot_topics in (
                    [ORDER_FILLED_TOPIC, None, wtopic],          # maker=wallet
                    [ORDER_FILLED_TOPIC, None, None, wtopic],    # taker=wallet
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
                logger.info(f"Chain feed [{tag}]: backfill processed {total} log(s)")
            # Advance watermark even when backfill found no new (or all-dedup'd)
            # events. Otherwise quiet periods cause repeated re-fetches of the
            # same range, and long outages (>BACKFILL_MAX_BLOCKS) would leak.
            if to_block > self._last_block:
                self._last_block = to_block
        except Exception as e:
            logger.warning(f"Chain feed [{tag}]: backfill failed: {e}")

    def _dedup_seen(self, key: tuple[str, str]) -> bool:
        """Return True if key was already processed. Otherwise mark seen."""
        now = time.time()
        if key in self._seen_logs:
            return True
        self._seen_logs[key] = now
        # TTL sweep + size cap (O(1) amortized).
        cutoff = now - DEDUP_TTL
        while self._seen_logs:
            oldest_key, oldest_ts = next(iter(self._seen_logs.items()))
            if oldest_ts < cutoff or len(self._seen_logs) > DEDUP_MAX:
                self._seen_logs.popitem(last=False)
            else:
                break
        return False

    def _handle_log(self, log: dict, source: str = ""):
        topics = log.get("topics", [])
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

        maker = "0x" + topics[2][-40:].lower()
        taker = "0x" + topics[3][-40:].lower()

        if maker in self.wallets:
            bs_side = "maker"
            wallet = maker
            counter = taker
        elif taker in self.wallets:
            bs_side = "taker"
            wallet = taker
            counter = maker
        else:
            return

        data_hex = log.get("data", "")
        if data_hex.startswith("0x"):
            data_hex = data_hex[2:]
        if len(data_hex) < 64 * 7:
            return

        side_enum = int(data_hex[0:64], 16)        # 0=BUY, 1=SELL (Order's signed side)
        token_id_int = int(data_hex[64:128], 16)
        maker_amt = int(data_hex[128:192], 16)
        taker_amt = int(data_hex[192:256], 16)
        # data[4]=fee, data[5]=builder, data[6]=metadata — unused here.

        if side_enum not in (0, 1) or token_id_int == 0:
            return

        # Order side determines amount semantics:
        #   BUY  → makerAmt = pUSD paid,    takerAmt = shares received
        #   SELL → makerAmt = shares given, takerAmt = pUSD received
        if side_enum == 0:
            shares_raw, usdc_raw = taker_amt, maker_amt
        else:
            shares_raw, usdc_raw = maker_amt, taker_amt

        # Wallet's actual direction:
        #   maker + BUY  → wallet bought
        #   maker + SELL → wallet sold
        #   taker + BUY  → wallet sold (it provided shares to the buyer)
        #   taker + SELL → wallet bought (it provided pUSD to the seller)
        wallet_buys = (bs_side == "maker") == (side_enum == 0)
        side = "buy" if wallet_buys else "sell"

        if shares_raw == 0:
            return
        shares = shares_raw / 1e6
        usdc = usdc_raw / 1e6
        price = usdc / shares if shares else 0.0
        asset_id = str(token_id_int)

        event = {
            "wallet": wallet,
            "asset": asset_id,
            "side": side,
            "size": shares,
            "price": price,
            "tx_hash": tx_hash,
            "block": block,
            "received_ts": time.time(),
        }
        logger.info(
            f"Chain[{source}]: {wallet[:8]}… {side.upper()} {shares:.2f} sh @ ${price:.4f} "
            f"asset={asset_id[:12]}… tx={tx_hash[:12]}…"
        )
        self.queue.put(event)

    def drain(self) -> list[dict]:
        """Pop all pending events non-blockingly."""
        out = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except Empty:
                break
        return out

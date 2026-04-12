"""
Background WebSocket feed for real-time CLOB prices.
Subscribes to the market channel and caches best bid/ask + last trade price.
Falls back gracefully: if WS is down, callers get None and can use REST.
"""
import asyncio
import json
import logging
import threading
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 9
PRICE_MAX_AGE = 120
TRADE_EVENT_MAX_AGE = 300


class WSPriceFeed:
    def __init__(self):
        self._prices: dict = {}
        self._recent_trades: dict = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._subscribed: set = set()
        self._pending_subs: list = []
        self._sub_lock = threading.Lock()

    def start(self, initial_assets: list[str]):
        with self._sub_lock:
            self._pending_subs = list(initial_assets)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_loop())

    async def _ws_loop(self):
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    with self._sub_lock:
                        initial = list(self._pending_subs)
                        self._pending_subs.clear()
                    if initial:
                        await self._send_subscribe(ws, initial)
                        self._subscribed.update(initial)

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    sub_task = asyncio.create_task(self._sub_checker(ws))

                    try:
                        async for raw in ws:
                            if raw == "PONG":
                                continue
                            self._handle_message(raw)
                    finally:
                        ping_task.cancel()
                        sub_task.cancel()
            except Exception as e:
                logger.warning(f"WS feed disconnected: {e}, reconnecting in 5s")
                await asyncio.sleep(5)

    async def _send_subscribe(self, ws, asset_ids):
        msg = json.dumps({
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        })
        await ws.send(msg)
        logger.info(f"WS price feed: subscribed to {len(asset_ids)} assets")

    async def _ping_loop(self, ws):
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                break

    async def _sub_checker(self, ws):
        while True:
            await asyncio.sleep(1)
            with self._sub_lock:
                new_subs = list(self._pending_subs)
                self._pending_subs.clear()
            if not new_subs:
                continue
            try:
                msg = json.dumps({
                    "assets_ids": new_subs,
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                })
                await ws.send(msg)
                self._subscribed.update(new_subs)
                logger.info(f"WS price feed: dynamically subscribed {len(new_subs)} new assets")
            except Exception:
                with self._sub_lock:
                    self._pending_subs.extend(new_subs)

    def _handle_message(self, raw):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self._update_from_event(item)
            return

        if isinstance(msg, dict):
            self._update_from_event(msg)

    def _update_from_event(self, msg: dict):
        asset_id = msg.get("asset_id", "")
        if not asset_id:
            return

        etype = msg.get("event_type", "")
        now = time.time()

        with self._lock:
            entry = self._prices.setdefault(asset_id, {})
            entry["ts"] = now

            if etype == "last_trade_price":
                entry["last"] = float(msg.get("price", 0))
                trades = self._recent_trades.setdefault(asset_id, [])
                trades.append({"ts": now, "price": entry["last"]})
                cutoff = now - TRADE_EVENT_MAX_AGE
                trades = [t for t in trades if t["ts"] > cutoff]
                if trades:
                    self._recent_trades[asset_id] = trades
                else:
                    del self._recent_trades[asset_id]
            elif etype == "best_bid_ask":
                bid = msg.get("best_bid")
                ask = msg.get("best_ask")
                if bid is not None:
                    entry["bid"] = float(bid)
                if ask is not None:
                    entry["ask"] = float(ask)
            elif etype == "book":
                bids = msg.get("bids", [])
                asks = msg.get("asks", [])
                if bids:
                    entry["bid"] = max(float(b.get("price", 0)) for b in bids)
                if asks:
                    entry["ask"] = min(float(a.get("price", 0)) for a in asks)

    def subscribe(self, asset_ids: list[str]):
        new = [a for a in asset_ids if a not in self._subscribed]
        if new:
            with self._sub_lock:
                self._pending_subs.extend(new)

    def has_recent_trade(self, asset_id: str, since_ts: float) -> bool:
        with self._lock:
            trades = self._recent_trades.get(asset_id, [])
            return any(t["ts"] >= since_ts for t in trades)

    def is_subscribed(self, asset_id: str) -> bool:
        return asset_id in self._subscribed

    def get_ask(self, asset_id: str) -> Optional[float]:
        with self._lock:
            entry = self._prices.get(asset_id)
            if not entry or time.time() - entry.get("ts", 0) > PRICE_MAX_AGE:
                return None
            return entry.get("ask") or entry.get("last")

    def get_bid(self, asset_id: str) -> Optional[float]:
        with self._lock:
            entry = self._prices.get(asset_id)
            if not entry or time.time() - entry.get("ts", 0) > PRICE_MAX_AGE:
                return None
            return entry.get("bid") or entry.get("last")

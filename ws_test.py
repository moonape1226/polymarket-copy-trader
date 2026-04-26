#!/usr/bin/env python3
"""
WebSocket latency test — compare WS market events vs REST polling
for detecting BeefSlayer's trades.

Run standalone:  python3 ws_test.py
"""
import asyncio
import json
import time
import requests
import websockets
from datetime import datetime, timezone

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
TARGET_WALLET = "0x331bf91c132af9d921e1908ca0979363fc47193f"
ACTIVITY_API = "https://data-api.polymarket.com/activity"
POSITIONS_API = "https://data-api.polymarket.com/positions"
PING_INTERVAL = 9  # seconds, server expects ping every 10s


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def fetch_target_assets() -> list[str]:
    """Get all asset_ids BeefSlayer currently holds."""
    resp = requests.get(POSITIONS_API, params={"user": TARGET_WALLET}, timeout=10)
    resp.raise_for_status()
    assets = []
    for p in resp.json():
        aid = p.get("asset", "")
        size = float(p.get("size", 0))
        if aid and size > 0:
            assets.append(aid)
    return assets


def fetch_recent_activity() -> dict:
    """Get latest activity timestamp per asset for the target wallet."""
    resp = requests.get(ACTIVITY_API, params={"user": TARGET_WALLET, "limit": 20}, timeout=10)
    resp.raise_for_status()
    latest = {}
    for r in resp.json():
        aid = r.get("asset", "")
        ts_val = int(r.get("timestamp", 0))
        if aid:
            latest[aid] = max(latest.get(aid, 0), ts_val)
    return latest


async def ws_listener(assets: list[str], event_log: list):
    """Connect to market WS, subscribe to target's assets, log trade events."""
    print(f"[{ts()}] Connecting to {WS_URL}")
    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        sub_msg = json.dumps({
            "assets_ids": assets,
            "type": "market",
            "custom_feature_enabled": True,
        })
        await ws.send(sub_msg)
        print(f"[{ts()}] Subscribed to {len(assets)} assets")

        async def ping_loop():
            while True:
                await asyncio.sleep(PING_INTERVAL)
                try:
                    await ws.send("PING")
                except Exception:
                    break

        ping_task = asyncio.create_task(ping_loop())

        try:
            async for raw in ws:
                if raw == "PONG":
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if isinstance(msg, list):
                    print(f"[{ts()}] WS batch: {len(msg)} item(s)")
                    continue

                etype = msg.get("event_type", "")
                if etype == "last_trade_price":
                    asset_id = msg.get("asset_id", "")[:16]
                    price = msg.get("price", "?")
                    size = msg.get("size", "?")
                    side = msg.get("side", "?")
                    now = time.time()
                    event_log.append({
                        "t": now,
                        "type": etype,
                        "asset": asset_id,
                        "price": price,
                        "size": size,
                        "side": side,
                    })
                    print(
                        f"[{ts()}] WS trade: {side} {size} @ {price}  "
                        f"asset={asset_id}..."
                    )
                elif etype in ("price_change", "best_bid_ask"):
                    pass  # noisy, skip
                elif etype == "book":
                    asset_id = msg.get("asset_id", "")[:16]
                    print(f"[{ts()}] WS book snapshot: asset={asset_id}...")
                else:
                    print(f"[{ts()}] WS event: {etype}")
        finally:
            ping_task.cancel()


async def rest_poller(poll_interval: float, event_log: list, stop_event: asyncio.Event):
    """Poll /activity every poll_interval seconds and log new trades."""
    known = fetch_recent_activity()
    print(f"[{ts()}] REST poller started (interval={poll_interval}s, baseline={len(known)} assets)")
    while not stop_event.is_set():
        await asyncio.sleep(poll_interval)
        try:
            current = fetch_recent_activity()
        except Exception as e:
            print(f"[{ts()}] REST poll error: {e}")
            continue
        for aid, ts_val in current.items():
            if ts_val > known.get(aid, 0):
                now = time.time()
                delay = now - ts_val
                event_log.append({
                    "t": now,
                    "type": "rest_detect",
                    "asset": aid[:16],
                    "activity_ts": ts_val,
                    "detect_delay": round(delay, 1),
                })
                print(
                    f"[{ts()}] REST detected trade on {aid[:16]}... "
                    f"(activity_ts={ts_val}, delay={delay:.1f}s)"
                )
        known = current


async def main():
    print(f"[{ts()}] Fetching BeefSlayer's current positions...")
    assets = fetch_target_assets()
    if not assets:
        print("No open positions found for target wallet.")
        return
    print(f"[{ts()}] Found {len(assets)} open assets")
    for a in assets:
        print(f"  {a[:20]}...")

    ws_events = []
    rest_events = []
    stop = asyncio.Event()

    print(f"\n[{ts()}] Starting WS listener + REST poller (Ctrl+C to stop)\n")
    print("=" * 70)

    try:
        await asyncio.gather(
            ws_listener(assets, ws_events),
            rest_poller(1.0, rest_events, stop),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()

    print(f"\n{'=' * 70}")
    print(f"WS events captured: {len(ws_events)}")
    print(f"REST detections: {len(rest_events)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")

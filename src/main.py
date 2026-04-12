#!/usr/bin/env python3
import time
import json
import logging
import os
import datetime
import requests
from ratelimit import limits, sleep_and_retry
from dotenv import load_dotenv

from src.positions import get_user_positions, detect_order_changes
from src.trading import TradingModule
from src.redeemer import redeem_resolved_positions
from src.notifier import send_portfolio_update
from src.ws_feed import WSPriceFeed

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)


def _fetch_recent_activity(wallet: str) -> list:
    """Fetch recent TRADE records (both BUY and SELL) from the Polymarket activity API."""
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": wallet, "limit": 50},
            timeout=10,
        )
        resp.raise_for_status()
        return [r for r in resp.json() if r.get("type") == "TRADE"]
    except Exception as e:
        logger.warning(f"Activity fetch failed for {wallet[:8]}…: {e}")
        return []


def main():
    config = load_config()
    wallets = config.get("wallets_to_track", [])
    rate_limit = config.get("rate_limit", 25)

    if not wallets:
        logger.error("No wallets to track in config.")
        return

    ws_feed = WSPriceFeed()
    initial_assets = []
    for wallet in wallets:
        positions = get_user_positions(wallet)
        if positions:
            for p in positions:
                aid = p.get("asset", "")
                if aid and float(p.get("size", 0)) > 0:
                    initial_assets.append(aid)
    ws_feed.start(initial_assets)
    logger.info(f"WS price feed started with {len(initial_assets)} assets")

    trading_module = TradingModule(config, ws_feed=ws_feed)

    @sleep_and_retry
    @limits(calls=rate_limit, period=10)
    def fetch_positions_safe(wallet_address):
        return get_user_positions(wallet_address)

    # Initialize state — pre-seed with [] so a fetch failure doesn't produce a
    # false-new-position baseline on the first successful poll.
    logger.info(f"Initializing state for {len(wallets)} wallets...")
    wallet_states = {wallet: [] for wallet in wallets}
    for wallet in wallets:
        positions = fetch_positions_safe(wallet)
        if positions is not None:
            wallet_states[wallet] = positions
            logger.info(f"Initialized {wallet[:8]}... with {len(positions)} positions")

    private_key    = os.getenv("POLYMARKET_PRIVATE_KEY")
    proxy_address  = os.getenv("POLYMARKET_PROXY_ADDRESS")
    slack_webhook  = os.getenv("SLACK_WEBHOOK_URL")
    redeem_interval = config.get("redeem_interval_cycles", 60)

    BATCH_WINDOW = config.get("batch_window_seconds", 180)
    MAX_PENDING  = config.get("max_pending_seconds", 300)
    # How long to wait for /activity confirmation before discarding as flicker.
    # 13–42s empirically; 60s gives comfortable margin.
    TRADE_CONFIRM_SECONDS = config.get("trade_confirm_seconds",
                                       config.get("sell_confirm_seconds", 60))
    ACTIVITY_POLL_INTERVAL = config.get("activity_poll_interval", 10)
    ACTIVITY_WINDOW = 300
    WS_LOOKBACK = 15

    activity_buys: dict = {}
    activity_sells: dict = {}
    last_activity_poll: float = 0.0

    logger.info(
        f"Starting copy trader loop (batch window: {BATCH_WINDOW}s, "
        f"confirm timeout: {TRADE_CONFIRM_SECONDS}s, activity poll: {ACTIVITY_POLL_INTERVAL}s, "
        f"WS dual-signal: enabled)..."
    )
    poll_cycle    = 0
    last_notify_slot = -1
    last_flush_time  = time.time()
    pending: dict = {}

    try:
        while True:
            for wallet in wallets:
                try:
                    current_positions = fetch_positions_safe(wallet)
                    if current_positions is None:
                        continue

                    previous_positions = wallet_states[wallet]
                    changes = detect_order_changes(previous_positions, current_positions)

                    for change in changes:
                        asset_id    = change["asset"]
                        size        = float(change["size"])
                        signed_size = size if change["type"].lower() == "buy" else -size

                        if asset_id not in pending:
                            pending[asset_id] = {
                                "net_size": 0.0, "price": change.get("price"), "meta": change,
                                "first_seen": time.time(), "ws_known": ws_feed.is_subscribed(asset_id),
                                "detection_price": ws_feed.get_ask(asset_id),
                            }
                            ws_feed.subscribe([asset_id])
                        pending[asset_id]["net_size"] += signed_size
                        pending[asset_id]["price"]     = change.get("price")
                        pending[asset_id]["meta"]      = change
                        price_str = f" @ ${float(change['price']):.3f}" if change.get('price') is not None else ""
                        logger.info(
                            f"Queued {change['type']} {size} shares of {change.get('title')}{price_str} "
                            f"(net: {pending[asset_id]['net_size']:+.2f})"
                        )

                    wallet_states[wallet] = current_positions

                except Exception as e:
                    logger.error(f"Error tracking {wallet}: {e}")

            now = time.time()

            # ── WS dual-signal: fast confirmation for WS-subscribed assets ──
            for asset_id in list(pending.keys()):
                p = pending[asset_id]
                if not p.get("ws_known"):
                    continue
                if not ws_feed.has_recent_trade(asset_id, p["first_seen"] - WS_LOOKBACK):
                    continue
                net = p["net_size"]
                if abs(net) < 0.01:
                    continue
                if net < -0.01:
                    synthetic = dict(p["meta"])
                    synthetic["type"] = "sell"
                    synthetic["size"] = abs(net)
                    synthetic["detection_price"] = p.get("detection_price")
                    logger.info(
                        f"WS confirmed sell: {abs(net):.2f} shares "
                        f"of {synthetic.get('title')}"
                    )
                    trading_module.execute_copy_trade(synthetic)
                    pending.pop(asset_id, None)
                elif not p.get("ws_confirmed"):
                    p["ws_confirmed"] = True
                    logger.info(
                        f"WS confirmed buy-add: {net:.2f} shares "
                        f"of {p['meta'].get('title')} (deferred to batch flush)"
                    )

            # ── Poll /activity to validate pending trades ────────────────────
            if now - last_activity_poll >= ACTIVITY_POLL_INTERVAL:
                for wallet in wallets:
                    for r in _fetch_recent_activity(wallet):
                        asset_id = r.get("asset", "")
                        ts = int(r.get("timestamp", 0))
                        side = r.get("side", "").upper()
                        if asset_id and ts:
                            if side == "BUY":
                                activity_buys[asset_id] = max(activity_buys.get(asset_id, 0), ts)
                            elif side == "SELL":
                                activity_sells[asset_id] = max(activity_sells.get(asset_id, 0), ts)
                cutoff = now - ACTIVITY_WINDOW
                activity_buys = {k: v for k, v in activity_buys.items() if v > cutoff}
                activity_sells = {k: v for k, v in activity_sells.items() if v > cutoff}
                last_activity_poll = now

                # Immediately flush confirmed pending trades
                for asset_id in list(pending.keys()):
                    p = pending[asset_id]
                    net = p["net_size"]
                    if net > 0.01 and activity_buys.get(asset_id, 0) > (now - ACTIVITY_WINDOW):
                        synthetic = dict(p["meta"])
                        synthetic["type"] = "buy"
                        synthetic["size"] = abs(net)
                        synthetic["detection_price"] = p.get("detection_price")
                        logger.info(
                            f"Flushing confirmed buy: {abs(net):.2f} shares "
                            f"of {synthetic.get('title')}"
                        )
                        trading_module.execute_copy_trade(synthetic)
                        pending.pop(asset_id, None)
                    elif net < -0.01 and activity_sells.get(asset_id, 0) > (now - ACTIVITY_WINDOW):
                        synthetic = dict(p["meta"])
                        synthetic["type"] = "sell"
                        synthetic["size"] = abs(net)
                        synthetic["detection_price"] = p.get("detection_price")
                        logger.info(
                            f"Flushing confirmed sell: {abs(net):.2f} shares "
                            f"of {synthetic.get('title')}"
                        )
                        trading_module.execute_copy_trade(synthetic)
                        pending.pop(asset_id, None)

            # ── Slack portfolio summary every hour ───────────────────────────
            _dt = datetime.datetime.now()
            current_slot = _dt.hour
            if slack_webhook and current_slot != last_notify_slot:
                last_notify_slot = current_slot
                try:
                    send_portfolio_update(proxy_address, slack_webhook)
                except Exception as e:
                    logger.error(f"Slack notification failed: {e}")

            # ── Flush batch every BATCH_WINDOW seconds ───────────────────────
            if now - last_flush_time >= BATCH_WINDOW:
                max_copy_pct = max(trading_module.copy_percentage, trading_module.low_prob_copy_percentage)
                to_remove = []

                # Split detection: skip BUY orders where both YES and NO were bought
                # at ~$0.50 with equal size (1 USDC = 1 YES + 1 NO split operation).
                SPLIT_PRICE_TOLERANCE = 0.05
                SPLIT_SIZE_TOLERANCE  = 0.01
                cid_buy_info: dict = {}
                for aid, p in pending.items():
                    if p["net_size"] > 0:
                        cid = p["meta"].get("conditionId")
                        outcome = (p["meta"].get("outcome") or "").lower()
                        price = p.get("price")
                        if cid and outcome in ("yes", "no") and price is not None:
                            cid_buy_info.setdefault(cid, {})[outcome] = {
                                "price": float(price),
                                "size": p["net_size"],
                            }
                hedged_cids: set = set()
                for cid, sides in cid_buy_info.items():
                    if "yes" in sides and "no" in sides:
                        yes_price, no_price = sides["yes"]["price"], sides["no"]["price"]
                        yes_size,  no_size  = sides["yes"]["size"],  sides["no"]["size"]
                        size_ratio = abs(yes_size - no_size) / max(yes_size, no_size)
                        if (abs(yes_price - 0.5) <= SPLIT_PRICE_TOLERANCE and
                                abs(no_price - 0.5) <= SPLIT_PRICE_TOLERANCE and
                                size_ratio <= SPLIT_SIZE_TOLERANCE):
                            logger.info(
                                f"Skipping split: both sides at ~$0.50 with equal qty "
                                f"({yes_size:.0f} YES / {no_size:.0f} NO) for conditionId {cid[:12]}…"
                            )
                            hedged_cids.add(cid)

                for asset_id, p in list(pending.items()):
                    net = p["net_size"]
                    if abs(net) < 0.01:
                        to_remove.append(asset_id)
                        continue

                    if net > 0 and p["meta"].get("conditionId") in hedged_cids:
                        to_remove.append(asset_id)
                        continue

                    price = p.get("price")
                    age = now - p["first_seen"]

                    # Carry forward small buys until they reach $1 or expire
                    if net > 0 and price is not None:
                        our_cost = abs(net) * max_copy_pct * float(price)
                        if our_cost < 1.0 and age < MAX_PENDING:
                            continue

                    synthetic = dict(p["meta"])
                    synthetic["type"] = "buy" if net > 0 else "sell"
                    synthetic["size"] = abs(net)
                    synthetic["detection_price"] = p.get("detection_price")

                    if age >= MAX_PENDING and net > 0 and price is not None:
                        our_cost = abs(net) * max_copy_pct * float(price)
                        if our_cost < 1.0:
                            logger.debug(f"Expiring pending: {synthetic.get('title')} (${our_cost:.2f} after {age:.0f}s)")
                            to_remove.append(asset_id)
                            continue

                    if p.get("ws_confirmed"):
                        logger.info(
                            f"Flushing WS-confirmed buy: {abs(net):.2f} shares "
                            f"of {synthetic.get('title')}"
                        )
                        trading_module.execute_copy_trade(synthetic)
                        to_remove.append(asset_id)
                    elif age < TRADE_CONFIRM_SECONDS:
                        direction = "sell" if net < 0 else "buy"
                        logger.info(
                            f"Holding {direction}: awaiting confirmation "
                            f"({age:.0f}s/{TRADE_CONFIRM_SECONDS}s) — {synthetic.get('title')}"
                        )
                        continue
                    else:
                        direction = "sell" if net < 0 else "buy"
                        logger.info(
                            f"Discarding {direction}: no confirmation after {age:.0f}s "
                            f"(flicker) — {synthetic.get('title')}"
                        )
                        to_remove.append(asset_id)
                        continue

                for aid in to_remove:
                    pending.pop(aid, None)
                last_flush_time = now

            poll_cycle += 1

            if poll_cycle % redeem_interval == 0:
                try:
                    redeemed = redeem_resolved_positions(private_key, proxy_address)
                    if redeemed:
                        logger.info(f"Redeemed {redeemed} resolved position(s)")
                except Exception as e:
                    logger.error(f"Redemption sweep failed: {e}")

            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Stopping...")

if __name__ == "__main__":
    main()

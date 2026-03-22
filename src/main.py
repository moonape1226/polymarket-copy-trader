#!/usr/bin/env python3
import time
import json
import logging
import os
from ratelimit import limits, sleep_and_retry
from dotenv import load_dotenv

from src.positions import get_user_positions, detect_order_changes
from src.trading import TradingModule
from src.redeemer import redeem_resolved_positions
from src.notifier import send_portfolio_update

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

def main():
    config = load_config()
    wallets = config.get("wallets_to_track", [])
    rate_limit = config.get("rate_limit", 25)
    
    if not wallets:
        logger.error("No wallets to track in config.")
        return

    trading_module = TradingModule(config)
    
    @sleep_and_retry
    @limits(calls=rate_limit, period=10)
    def fetch_positions_safe(wallet_address):
        return get_user_positions(wallet_address)
    
    # Initialize state — pre-seed with [] so a fetch failure doesn't produce a
    # false-new-position baseline on the first successful poll (fix #1).
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

    BATCH_WINDOW = config.get("batch_window_seconds", 180)  # 3 minutes

    logger.info(f"Starting copy trader loop (batch window: {BATCH_WINDOW}s)...")
    poll_cycle    = 0
    last_notify_time = 0
    last_flush_time  = time.time()
    # pending[asset_id] = {"net_size": float, "price": float, "meta": change_dict}
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
                            pending[asset_id] = {"net_size": 0.0, "price": change.get("price"), "meta": change}
                        pending[asset_id]["net_size"] += signed_size
                        pending[asset_id]["price"]     = change.get("price")   # keep latest price
                        pending[asset_id]["meta"]      = change                # keep latest metadata
                        logger.info(
                            f"Queued {change['type']} {size} shares of {change.get('title')} "
                            f"(net: {pending[asset_id]['net_size']:+.2f})"
                        )

                    wallet_states[wallet] = current_positions

                except Exception as e:
                    logger.error(f"Error tracking {wallet}: {e}")

            # ── Flush batch every BATCH_WINDOW seconds ──────────────────────
            now = time.time()
            if now - last_flush_time >= BATCH_WINDOW:
                for asset_id, p in list(pending.items()):
                    net = p["net_size"]
                    if abs(net) < 0.01:
                        continue
                    synthetic = dict(p["meta"])
                    synthetic["type"] = "buy" if net > 0 else "sell"
                    synthetic["size"] = abs(net)
                    logger.info(
                        f"Flushing batch: net {synthetic['type']} {synthetic['size']:.2f} shares "
                        f"of {synthetic.get('title')}"
                    )
                    trading_module.execute_copy_trade(synthetic)
                pending.clear()
                last_flush_time = now

            poll_cycle += 1

            # Hourly Slack portfolio update
            if slack_webhook:
                now = time.time()
                if now - last_notify_time >= 3600:
                    try:
                        send_portfolio_update(proxy_address, slack_webhook)
                        last_notify_time = now
                    except Exception as e:
                        logger.error(f"Slack notification failed: {e}")

            if poll_cycle % redeem_interval == 0:
                try:
                    redeemed = redeem_resolved_positions(private_key, proxy_address)
                    if redeemed:
                        logger.info(f"Redeemed {redeemed} resolved position(s)")
                except Exception as e:
                    logger.error(f"Redemption sweep failed: {e}")

            time.sleep(1) # Check every second (rate limiter handles API constraint)

    except KeyboardInterrupt:
        logger.info("Stopping...")

if __name__ == "__main__":
    main()

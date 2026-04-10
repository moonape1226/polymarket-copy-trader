#!/usr/bin/env python3
import time
import json
import logging
import os
import datetime
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
    # Tracks how many consecutive polls each asset has been absent per wallet.
    # A position must be missing for 2+ consecutive polls before being treated as a real sell,
    # filtering single-poll API glitches where positions temporarily disappear.
    absent_counts: dict = {wallet: {} for wallet in wallets}
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
    MAX_PENDING  = config.get("max_pending_seconds", 300)   # 5 minutes

    logger.info(f"Starting copy trader loop (batch window: {BATCH_WINDOW}s, max pending: {MAX_PENDING}s)...")
    poll_cycle    = 0
    last_notify_slot = -1   # tracks last notified 30-min slot (hour*2 + 0/1)
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
                    curr_map = {p['asset']: p for p in current_positions}
                    prev_map = {p['asset']: p for p in previous_positions}

                    # Update absent counts: reset for returned assets, increment for missing ones
                    for asset_id in list(absent_counts[wallet].keys()):
                        if asset_id in curr_map:
                            del absent_counts[wallet][asset_id]
                    for asset_id in prev_map:
                        if asset_id not in curr_map:
                            absent_counts[wallet][asset_id] = absent_counts[wallet].get(asset_id, 0) + 1

                    # Build stable view: ghost positions absent for only 1 poll back in,
                    # so single-poll API dropouts don't generate spurious SELL signals.
                    stable_current = list(current_positions)
                    for asset_id, count in absent_counts[wallet].items():
                        if count < 2 and asset_id in prev_map:
                            stable_current.append(prev_map[asset_id])
                            logger.debug(f"Holding ghost position {asset_id[:12]}… (absent {count} poll)")

                    changes = detect_order_changes(previous_positions, stable_current)

                    # Clean up absent counts for assets confirmed sold this cycle
                    for change in changes:
                        if change["type"].upper() == "SELL":
                            absent_counts[wallet].pop(change["asset"], None)

                    for change in changes:
                        asset_id    = change["asset"]
                        size        = float(change["size"])
                        signed_size = size if change["type"].lower() == "buy" else -size

                        if asset_id not in pending:
                            pending[asset_id] = {"net_size": 0.0, "price": change.get("price"), "meta": change, "first_seen": time.time()}
                        pending[asset_id]["net_size"] += signed_size
                        pending[asset_id]["price"]     = change.get("price")   # keep latest price
                        pending[asset_id]["meta"]      = change                # keep latest metadata
                        price_str = f" @ ${float(change['price']):.3f}" if change.get('price') is not None else ""
                        logger.info(
                            f"Queued {change['type']} {size} shares of {change.get('title')}{price_str} "
                            f"(net: {pending[asset_id]['net_size']:+.2f})"
                        )

                    wallet_states[wallet] = stable_current

                except Exception as e:
                    logger.error(f"Error tracking {wallet}: {e}")

            # ── Flush batch every BATCH_WINDOW seconds ──────────────────────
            now = time.time()
            _dt = datetime.datetime.now()
            current_slot = _dt.hour * 2 + (1 if _dt.minute >= 30 else 0)
            if slack_webhook and current_slot != last_notify_slot:
                last_notify_slot = current_slot  # advance first; don't retry every second on failure
                try:
                    send_portfolio_update(proxy_address, slack_webhook)
                except Exception as e:
                    logger.error(f"Slack notification failed: {e}")

            if now - last_flush_time >= BATCH_WINDOW:
                max_copy_pct = max(trading_module.copy_percentage, trading_module.low_prob_copy_percentage)
                to_remove = []

                # ── Split detection: skip BUY orders that are a Split (both sides at ~$0.50, equal qty) ──
                # A Split always prices YES and NO at exactly $0.50 each (1 USDC = 1 YES + 1 NO)
                # and produces identical quantities. A deliberate hedge has different prices or sizes.
                SPLIT_PRICE_TOLERANCE = 0.05
                SPLIT_SIZE_TOLERANCE  = 0.01  # relative tolerance for size equality
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
                # ─────────────────────────────────────────────────────────────────────

                for asset_id, p in list(pending.items()):
                    net = p["net_size"]
                    if abs(net) < 0.01:
                        to_remove.append(asset_id)
                        continue

                    # Skip split/merge wash trades
                    if net > 0 and p["meta"].get("conditionId") in hedged_cids:
                        to_remove.append(asset_id)
                        continue

                    price = p.get("price")
                    age = now - p["first_seen"]

                    # Carry forward buys that are too small for a $1 order (unless expired)
                    if net > 0 and price is not None:
                        our_cost = abs(net) * max_copy_pct * float(price)
                        if our_cost < 1.0 and age < MAX_PENDING:
                            continue

                    synthetic = dict(p["meta"])
                    synthetic["type"] = "buy" if net > 0 else "sell"
                    synthetic["size"] = abs(net)
                    if age >= MAX_PENDING and net > 0 and price is not None:
                        our_cost = abs(net) * max_copy_pct * float(price)
                        if our_cost < 1.0:
                            logger.debug(f"Expiring pending: {synthetic.get('title')} (${our_cost:.2f} after {age:.0f}s)")
                            to_remove.append(asset_id)
                            continue
                    logger.info(
                        f"Flushing batch: net {synthetic['type']} {synthetic['size']:.2f} shares "
                        f"of {synthetic.get('title')}"
                    )
                    trading_module.execute_copy_trade(synthetic)
                    to_remove.append(asset_id)

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

            time.sleep(1) # Check every second (rate limiter handles API constraint)

    except KeyboardInterrupt:
        logger.info("Stopping...")

if __name__ == "__main__":
    main()

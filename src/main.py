#!/usr/bin/env python3
import time
import json
import logging
import os
import datetime
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from ratelimit import limits, sleep_and_retry
from dotenv import load_dotenv

from src.positions import get_user_positions, detect_order_changes
from src.trading import TradingModule
from src.redeemer import redeem_resolved_positions
from src.notifier import send_portfolio_update
from src.ws_feed import WSPriceFeed
from src.chain_feed import ChainFeed

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

    chain_feed = ChainFeed(wallets)
    chain_feed.start()

    trading_module = TradingModule(config, ws_feed=ws_feed)
    trading_module.backfill_bs_cost_basis(wallets)

    @sleep_and_retry
    @limits(calls=rate_limit, period=10)
    def fetch_positions_safe(wallet_address):
        return get_user_positions(wallet_address)

    # Initialize state. A fetch failure leaves the wallet uninitialized; the
    # first successful fetch becomes the baseline and is not diffed.
    logger.info(f"Initializing state for {len(wallets)} wallets...")
    wallet_states = {wallet: None for wallet in wallets}
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

    executor = ThreadPoolExecutor(max_workers=5)
    asset_locks: dict = {}
    asset_locks_guard = threading.Lock()
    recently_dispatched: dict = {}  # (asset_id, side) → dispatch ts; per-side so a
                                    # chain BUY doesn't silence polling's SELL detection
                                    # when BS flips the same asset within CHAIN_DEDUP_TTL.
    RECONCILE_MAX_AGE = 960  # 16 minutes

    # Chain-feed aggregation: collect OrderFilled events per (asset, side) and flush
    # after CHAIN_QUIESCE seconds of no new events. Handles multi-fill orders that
    # emit several OrderFilled logs within the same tx.
    chain_pending: dict = {}  # (asset, side) → {size, price_wsum, usd_sum, first_ts, last_ts, tx_hashes, wallet}
    CHAIN_QUIESCE = float(config.get("chain_quiesce_seconds", 1.5))
    CHAIN_DEDUP_TTL = 300  # seconds: /positions detection is suppressed this long after chain dispatch
    BS_SELL_REBUY_COOLOFF = 60  # seconds: upper bound on /positions lag vs chain feed

    # Independent metadata cache for chain-path lookups. Does NOT write to
    # wallet_states (which would corrupt polling's diff baseline). Populated
    # from wallet_states on-demand, and refreshed by a one-shot sync fetch
    # when chain_feed fires on a brand-new asset BS just opened.
    asset_metadata_cache: dict = {}  # asset_id → {title, outcome, conditionId, slug}

    def _get_asset_lock(aid: str) -> threading.Lock:
        with asset_locks_guard:
            lk = asset_locks.get(aid)
            if lk is None:
                lk = threading.Lock()
                asset_locks[aid] = lk
            return lk

    def _run_trade(synth: dict):
        aid = synth.get("asset", "")
        side = (synth.get("type") or "").lower()
        recently_dispatched[(aid, side)] = time.time()
        lock = _get_asset_lock(aid)
        with lock:
            try:
                result = trading_module.execute_copy_trade(synth)
                if result is False:
                    recently_dispatched.pop((aid, side), None)
            except Exception as e:
                recently_dispatched.pop((aid, side), None)
                logger.error(f"execute_copy_trade failed for {aid[:12]}…: {e}")

    def _dispatch(synth: dict):
        executor.submit(_run_trade, synth)

    def _metadata_from_states(asset_id: str) -> dict:
        """Lookup title/outcome/conditionId from known BS positions.
        Only caches and returns metadata with a non-empty title — a dict with
        title=None is treated as a miss so downstream skip logic triggers.
        """
        cached = asset_metadata_cache.get(asset_id)
        if cached:
            return cached
        for w, positions in wallet_states.items():
            for p in positions or []:
                if p.get("asset") == asset_id:
                    title = p.get("title")
                    if not title:
                        continue
                    meta = {
                        "title": title,
                        "outcome": p.get("outcome"),
                        "conditionId": p.get("conditionId"),
                        "slug": p.get("slug"),
                    }
                    asset_metadata_cache[asset_id] = meta
                    return meta
        return {}

    def _fetch_metadata_from_gamma(asset_id: str) -> dict:
        """Resolve chain-feed asset metadata via gamma-api.

        Gamma indexes markets at creation, so it answers for assets that
        data-api /positions hasn't picked up yet (BS just opened a new
        position; data-api lags ~10s). Returns {} on any failure or if the
        market is closed/archived; caller treats {} as a metadata miss and
        lets polling handle it.
        """
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"clob_token_ids": asset_id},
                timeout=5,
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as e:
            logger.warning(f"Gamma metadata fetch failed for {asset_id[:12]}…: {e}")
            return {}
        if not markets:
            return {}
        m = markets[0]
        if m.get("closed") or m.get("archived"):
            return {}
        title = m.get("question")
        if not title:
            return {}
        tids = m.get("clobTokenIds")
        outs = m.get("outcomes")
        try:
            if isinstance(tids, str): tids = json.loads(tids)
            if isinstance(outs, str): outs = json.loads(outs)
        except Exception:
            tids, outs = None, None
        outcome = None
        if isinstance(tids, list) and isinstance(outs, list) and asset_id in tids:
            idx = tids.index(asset_id)
            if 0 <= idx < len(outs):
                outcome = outs[idx]
        return {
            "title": title,
            "outcome": outcome,
            "conditionId": m.get("conditionId"),
            "slug": m.get("slug"),
        }

    def _drain_chain_events():
        """Pull OrderFilled events from chain_feed, aggregate per (asset, side),
        and flush after CHAIN_QUIESCE seconds of quiet.
        """
        now_ts = time.time()
        for ev in chain_feed.drain():
            key = (ev["asset"], ev["side"])
            bucket = chain_pending.get(key)
            if bucket is None:
                bucket = {
                    "wallet": ev["wallet"],
                    "asset": ev["asset"],
                    "side": ev["side"],
                    "shares": 0.0,
                    "usd": 0.0,
                    "first_ts": ev["received_ts"],
                    "last_ts": ev["received_ts"],
                    "tx_hashes": set(),
                    "block": ev["block"],
                }
                chain_pending[key] = bucket
            bucket["shares"] += ev["size"]
            bucket["usd"] += ev["size"] * ev["price"]
            bucket["last_ts"] = ev["received_ts"]
            bucket["tx_hashes"].add(ev["tx_hash"])

        # Collect buckets ready to flush (quiet ≥ CHAIN_QUIESCE seconds).
        ready_keys = [
            k for k, b in chain_pending.items()
            if now_ts - b["last_ts"] >= CHAIN_QUIESCE and b["shares"] >= 1e-6
        ]
        # Drop dust buckets eagerly
        for k in list(chain_pending.keys()):
            if chain_pending[k]["shares"] < 1e-6 and now_ts - chain_pending[k]["last_ts"] >= CHAIN_QUIESCE:
                chain_pending.pop(k, None)

        # Metadata miss resolution: gamma-api lookup per asset. Gamma indexes
        # markets at creation so it covers BS-just-opened assets that data-api
        # /positions hasn't indexed yet (~10s lag). Cache hits skip the call.
        for k in ready_keys:
            b = chain_pending[k]
            if _metadata_from_states(b["asset"]).get("title"):
                continue
            if b["asset"] in asset_metadata_cache:
                continue
            meta = _fetch_metadata_from_gamma(b["asset"])
            if meta.get("title"):
                asset_metadata_cache[b["asset"]] = meta

        chain_meta: dict = {}
        by_condition: dict = {}
        for k in ready_keys:
            b = chain_pending.get(k)
            if b is None:
                continue
            meta = _metadata_from_states(b["asset"])
            if not meta.get("title"):
                continue
            chain_meta[k] = meta
            outcome = (meta.get("outcome") or "").lower()
            cid = meta.get("conditionId")
            if not cid or outcome not in ("yes", "no"):
                continue
            shares = b["shares"]
            avg_price = b["usd"] / shares if shares else 0.0
            by_condition.setdefault(cid, {}).setdefault(b["side"], {})[outcome] = {
                "key": k, "price": avg_price, "size": shares, "title": meta.get("title"),
            }

        skip_chain_keys: set = set()
        for cid, sides in by_condition.items():
            for side, outcomes in sides.items():
                if "yes" not in outcomes or "no" not in outcomes:
                    continue
                yes, no = outcomes["yes"], outcomes["no"]
                size_ratio = abs(yes["size"] - no["size"]) / max(yes["size"], no["size"])
                if side == "buy":
                    is_skip = (
                        abs(yes["price"] - 0.5) <= 0.05 and
                        abs(no["price"] - 0.5) <= 0.05 and
                        size_ratio <= 0.01
                    )
                    label = "split"
                else:
                    is_skip = size_ratio <= 0.01
                    label = "merge"
                if is_skip:
                    logger.info(
                        f"Chain skip {label}: equal YES/NO {side}s "
                        f"({yes['size']:.2f} / {no['size']:.2f}) for {yes.get('title') or cid[:12]+'…'}"
                    )
                    skip_chain_keys.update([yes["key"], no["key"]])

        for key in ready_keys:
            b = chain_pending.get(key)
            if b is None:
                continue
            if key in skip_chain_keys:
                chain_pending.pop(key, None)
                pending.pop(b["asset"], None)
                continue
            shares = b["shares"]
            avg_price = b["usd"] / shares if shares else 0.0
            meta = chain_meta.get(key) or _metadata_from_states(b["asset"])
            if not meta.get("title"):
                # Still no metadata after sync fetch — data-api likely hasn't
                # indexed yet. Skip; polling will pick it up within ~1s.
                logger.warning(
                    f"Chain-flush skip (metadata miss after sync): "
                    f"{b['side'].upper()} {shares:.2f} sh of asset={b['asset'][:12]}… "
                    f"— letting polling handle it"
                )
                chain_pending.pop(key, None)
                continue
            synth = {
                "asset": b["asset"],
                "type": b["side"],  # "buy" or "sell"
                "size": shares,
                "price": avg_price,
                "detection_price": avg_price,
                **meta,
            }
            detect_latency = now_ts - b["first_ts"]
            logger.info(
                f"Chain-flush {b['side'].upper()}: {shares:.2f} sh @ ${avg_price:.4f} "
                f"of {meta.get('title') or b['asset'][:12]+'…'} "
                f"(wallet={b['wallet'][:8]}…, latency={detect_latency:.2f}s, "
                f"{len(b['tx_hashes'])} tx)"
            )
            try:
                trading_module.update_bs_cost_basis({
                    "asset": b["asset"], "type": b["side"].upper(),
                    "size": shares, "price": avg_price,
                })
            except Exception as e:
                logger.warning(f"update_bs_cost_basis failed for chain event: {e}")
            trading_module._log_copy_decision(synth, "CHAIN_DETECT")
            recently_dispatched[(b["asset"], b["side"])] = now_ts
            _dispatch(synth)
            # Clear any /positions-derived pending for this asset to prevent double-fire
            pending.pop(b["asset"], None)
            chain_pending.pop(key, None)

    def _reconcile_missed_copies():
        """Find assets BS holds but we don't, with recent trades within 16 min, and backfill."""
        now_ts = time.time()
        for k in list(recently_dispatched.keys()):
            if (now_ts - recently_dispatched[k]) >= CHAIN_DEDUP_TTL:
                recently_dispatched.pop(k, None)
        # Reconcile only handles missed BUYs, so check the buy-side key.
        try:
            our_positions = get_user_positions(proxy_address) or []
        except Exception as e:
            logger.warning(f"Reconcile: fetch our positions failed: {e}")
            return
        our_assets = {p.get("asset") for p in our_positions
                       if float(p.get("size", 0)) > 0}
        for wallet in wallets:
            bs_positions = wallet_states.get(wallet) or []
            activity = _fetch_recent_activity(wallet)
            last_ts_map: dict = {}
            for r in activity:
                aid = r.get("asset", "")
                ts = int(r.get("timestamp", 0))
                if aid and ts:
                    last_ts_map[aid] = max(last_ts_map.get(aid, 0), ts)
            # Build on-chain share map so reconcile can dispatch delta (not full bs_size).
            # Previous behaviour re-dispatched bs_size every 60-120s when on-chain lag
            # hadn't caught up, causing 4-6× over-buy (see NYC 60-61 $4.9k exposure).
            our_share_map = {p.get("asset"): float(p.get("size", 0)) for p in our_positions}
            for bs_pos in bs_positions:
                aid = bs_pos.get("asset")
                bs_size = float(bs_pos.get("size", 0))
                if not aid or bs_size <= 0:
                    continue
                if aid in pending:
                    continue
                if (aid, "buy") in recently_dispatched:
                    continue
                # Chain feed dispatches SELL ~3-5s after on-chain, but the
                # /positions snapshot (driving bs_size below) can lag 30s+;
                # without this guard reconcile re-buys what we just sold.
                sell_ts = recently_dispatched.get((aid, "sell"))
                if sell_ts and (now_ts - sell_ts) < BS_SELL_REBUY_COOLOFF:
                    logger.info(
                        f"Reconcile skip: BS sold {now_ts - sell_ts:.0f}s ago "
                        f"— {bs_pos.get('title')}"
                    )
                    continue
                last_ts = last_ts_map.get(aid, 0)
                if not last_ts or (now_ts - last_ts) > RECONCILE_MAX_AGE:
                    continue
                # Compute delta: bs_size - our on-chain - our pending GTC order
                our_on_chain = our_share_map.get(aid, 0.0)
                pending_shares = trading_module.get_pending_order_shares(aid)
                missed = bs_size - our_on_chain - pending_shares
                if missed < 1.0:
                    continue
                # Safety fuse: skip reconcile if current ask is >2x BS avg — stale
                # catch-up should not chase a runaway market (would trigger Plan B
                # market-order sweep and fill at top of book; see NYC 54-55 incident).
                bs_avg = bs_pos.get("avgPrice")
                cur_ask = ws_feed.get_ask(aid)
                if bs_avg and cur_ask and float(bs_avg) > 0 and cur_ask > float(bs_avg) * 2:
                    logger.info(
                        f"Reconcile skip: ask {cur_ask:.4f} > 2x BS avg {float(bs_avg):.4f} "
                        f"— {bs_pos.get('title')}"
                    )
                    continue
                # Reconcile ref price: when market has fallen below BS's historical
                # avg (e.g. BS averaged in at $0.20 but is now buying @ $0.02),
                # using bs_avg overpays by multiples. Cap at current ask so we
                # don't chase stale prices while catching up old shortfalls.
                ref_price = float(bs_avg) if bs_avg else None
                if cur_ask and cur_ask > 0 and ref_price and ref_price > 0:
                    ref_price = min(ref_price, float(cur_ask))
                synth = {
                    "asset": aid,
                    "type": "buy",
                    "size": missed,
                    "price": ref_price,
                    "title": bs_pos.get("title"),
                    "outcome": bs_pos.get("outcome"),
                    "conditionId": bs_pos.get("conditionId"),
                    "detection_price": ref_price,
                }
                age_s = now_ts - last_ts
                logger.info(
                    f"Reconcile: catching up missed buy "
                    f"{bs_pos.get('title')} — {missed:.2f} sh "
                    f"(bs={bs_size:.2f} on_chain={our_on_chain:.2f} pending={pending_shares:.2f}, age {age_s:.0f}s)"
                )
                trading_module._log_copy_decision(synth, "RECONCILE_BACKFILL")
                _dispatch(synth)

    try:
        while True:
            # Drain chain_feed events first — they're the fastest signal and let us
            # suppress the /positions detection path via recently_dispatched.
            try:
                _drain_chain_events()
            except Exception as e:
                logger.error(f"Chain feed drain failed: {e}")

            for wallet in wallets:
                try:
                    current_positions = fetch_positions_safe(wallet)
                    if current_positions is None:
                        continue

                    previous_positions = wallet_states[wallet]
                    if previous_positions is None:
                        wallet_states[wallet] = current_positions
                        logger.info(
                            f"Initialized {wallet[:8]}... with {len(current_positions)} positions after fetch recovery"
                        )
                        continue
                    changes = detect_order_changes(previous_positions, current_positions)

                    for change in changes:
                        asset_id    = change["asset"]
                        size        = float(change["size"])
                        side_lc = change["type"].lower()
                        signed_size = size if side_lc == "buy" else -size
                        # Skip only if chain_feed already dispatched the SAME side recently
                        rd_ts = recently_dispatched.get((asset_id, side_lc))
                        if rd_ts and (time.time() - rd_ts) < CHAIN_DEDUP_TTL:
                            continue

                        if asset_id not in pending:
                            pending[asset_id] = {
                                "net_size": 0.0, "price": change.get("price"), "meta": change,
                                "first_seen": time.time(), "ws_known": ws_feed.is_subscribed(asset_id),
                                "detection_price": ws_feed.get_ask(asset_id),
                            }
                            ws_feed.subscribe([asset_id])
                            trading_module._log_copy_decision(change, "QUEUED")
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

            # Sells are dispatched through the activity-confirmed flush below
            # (see ~L510). The previous "immediate sell" bypass fired on any
            # polled net<-0.01 and was vulnerable to API flickers (e.g. 2026-04-23
            # phantom sell of Atlanta 84-85 after a BS BUY triggered -10 sh poll).

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
                if not p.get("ws_confirmed") and net > 0.01:
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
                                prev = activity_buys.get(asset_id)
                                if not prev or ts > prev["ts"]:
                                    activity_buys[asset_id] = {"ts": ts, "price": float(r.get("price", 0))}
                            elif side == "SELL":
                                activity_sells[asset_id] = max(activity_sells.get(asset_id, 0), ts)
                cutoff = now - ACTIVITY_WINDOW
                activity_buys = {k: v for k, v in activity_buys.items() if v["ts"] > cutoff}
                activity_sells = {k: v for k, v in activity_sells.items() if v > cutoff}
                last_activity_poll = now

                # Immediately flush confirmed pending trades
                for asset_id in list(pending.keys()):
                    p = pending[asset_id]
                    net = p["net_size"]
                    buy_info = activity_buys.get(asset_id)
                    # Stale-activity guard: if activity ts is much older than this pending,
                    # it's from an earlier buy on the same asset — don't use its price.
                    if buy_info and (p["first_seen"] - buy_info["ts"]) > 60:
                        buy_info = None
                    sell_ts = activity_sells.get(asset_id)
                    if sell_ts and (p["first_seen"] - sell_ts) > 60:
                        sell_ts = None
                    if net > 0.01 and buy_info:
                        target_price = buy_info["price"]
                        synthetic = dict(p["meta"])
                        synthetic["type"] = "buy"
                        synthetic["size"] = abs(net)
                        synthetic["price"] = target_price
                        synthetic["detection_price"] = target_price or p.get("detection_price")
                        logger.info(
                            f"Flushing confirmed buy: {abs(net):.2f} shares "
                            f"of {synthetic.get('title')} (target@${target_price:.4f})"
                        )
                        try:
                            trading_module.update_bs_cost_basis(synthetic)
                        except Exception as e:
                            logger.warning(f"update_bs_cost_basis failed for confirmed buy: {e}")
                        _dispatch(synthetic)
                        pending.pop(asset_id, None)
                    elif net < -0.01 and sell_ts:
                        synthetic = dict(p["meta"])
                        synthetic["type"] = "sell"
                        synthetic["size"] = abs(net)
                        synthetic["detection_price"] = p.get("detection_price")
                        logger.info(
                            f"Flushing confirmed sell: {abs(net):.2f} shares "
                            f"of {synthetic.get('title')}"
                        )
                        try:
                            trading_module.update_bs_cost_basis(synthetic)
                        except Exception as e:
                            logger.warning(f"update_bs_cost_basis failed for confirmed sell: {e}")
                        _dispatch(synthetic)
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
                            trading_module._log_copy_decision(
                                p["meta"], "FLUSH_DEFERRED", our_cost=round(our_cost, 4)
                            )
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
                        try:
                            trading_module.update_bs_cost_basis(synthetic)
                        except Exception as e:
                            logger.warning(f"update_bs_cost_basis failed for WS-confirmed buy: {e}")
                        _dispatch(synthetic)
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

                try:
                    _reconcile_missed_copies()
                except Exception as e:
                    logger.error(f"Reconcile failed: {e}")

            try:
                trading_module.check_pending_sells()
            except Exception as e:
                logger.error(f"check_pending_sells failed: {e}")

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

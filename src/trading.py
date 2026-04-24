import csv
import logging
import os
import json
import threading
import time
import requests as http_requests
import pmxt
import pmxt.server_manager as _sm
from dotenv import load_dotenv
from typing import Dict, Any, Optional
from src.positions import get_user_positions
from web3 import Web3

load_dotenv()

logger = logging.getLogger(__name__)


_BOT_TRADES_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "bot_trades.csv")
_BOT_TRADES_FIELDS = [
    "timestamp", "side", "title", "outcome", "asset_id", "condition_id",
    "our_shares", "price", "our_cost", "copy_pct", "is_low_prob", "order_id",
]
_GTC_CANCELLED_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "gtc_cancelled.csv")
_GTC_CANCELLED_FIELDS = [
    "timestamp", "asset_id", "title", "limit_price", "placed_at", "cancelled_after_min", "reason",
]
_COPY_DECISIONS_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "copy_decisions.csv")
_COPY_DECISIONS_FIELDS = [
    "timestamp", "action", "title", "outcome", "condition_id", "asset_id",
    "bs_price", "bs_size", "bs_usd", "our_price", "our_size", "our_cost", "order_id",
]
_MARKET_CACHE_MAXSIZE = 1000
_RPC_URL = "https://polygon-bor-rpc.publicnode.com"
_USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
_USDC_ABI = [{"name": "balanceOf", "type": "function",
              "inputs": [{"name": "account", "type": "address"}],
              "outputs": [{"type": "uint256"}], "stateMutability": "view"}]


class TradingModule:
    def __init__(self, config: Dict[str, Any], ws_feed=None):
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        if not private_key:
            raise EnvironmentError("POLYMARKET_PRIVATE_KEY is not set")
        if not proxy_address:
            raise EnvironmentError("POLYMARKET_PROXY_ADDRESS is not set")

        self.config = config
        self.copy_percentage = max(0.0, min(float(config.get("copy_percentage", 1.0)), 1.0))
        self.low_prob_copy_percentage = max(0.0, min(float(config.get("low_prob_copy_percentage", self.copy_percentage)), 1.0))
        self.low_prob_price_threshold = float(config.get("low_prob_price_threshold", 0.30))
        self.low_prob_min_trade_usd = float(config.get("low_prob_min_trade_usd", 0))
        self.low_prob_max_portfolio_pct = float(config.get("low_prob_max_portfolio_pct", 1.0))
        self.low_prob_max_order_usd = float(config.get("low_prob_max_order_usd", 0))
        self.min_target_shares = float(config.get("min_target_shares", 0))
        self.max_buy_price = config.get("max_buy_price")
        self.min_trade_usd = float(config.get("min_trade_usd", 0))
        self.entry_price_multiplier = float(config.get("entry_price_multiplier", 1.5))
        self.market_order_gap_threshold = float(config.get("market_order_gap_threshold", 1.05))
        # Slip buffer: limit = BS_avg × (1 + slip). BS's winning trades tend to be
        # sweeps that walk asks up; a limit at exact BS_avg never fills because
        # the next ask sits above BS's last fill. 0.08 ≈ $0.03 slippage per share
        # on typical mid-price entries, in exchange for actually entering the trade.
        self.buy_limit_slip_pct = float(config.get("buy_limit_slip_pct", 0.0))
        self.unknown_price_max_ask = float(config.get("unknown_price_max_ask", 0.15))
        self.gtc_order_ttl = float(config.get("gtc_order_ttl_minutes", 120)) * 60
        self._proxy_address = proxy_address
        self.max_position_usd = float(config.get("max_position_usd", 0))
        self._market_cache: Dict[str, Optional[str]] = {}
        self._asset_copy_rate: Dict[str, float] = {}   # asset_id → copy_pct used on buy
        self._asset_is_low_prob: Dict[str, bool] = {}  # asset_id → was it a low-prob buy
        self._asset_exposure: Dict[str, float] = {}    # asset_id → USD spent on our copy
        self._asset_shares: Dict[str, float] = {}      # asset_id → shares we hold
        self._low_prob_exposure: float = 0.0           # USD currently in low-prob positions
        self._exposure_last_refresh: float = 0.0       # timestamp of last exposure refresh
        self._usdc_balance_cached: float = 0.0
        self._usdc_balance_refresh: float = 0.0
        self._pending_order_ids: Dict[str, str] = {}    # asset_id → unfilled GTC buy order_id
        self._pending_order_times: Dict[str, float] = {}   # asset_id → order placement timestamp
        self._pending_order_meta: Dict[str, dict] = {}     # asset_id → {limit_price, title}
        self._pending_order_shares: Dict[str, float] = {}  # asset_id → unfilled shares on pending order
        self._pending_order_cost: Dict[str, float] = {}    # asset_id → USD cost of pending order
        # Sell-side maker-with-TTL tracking (profit: long TTL, loss: short TTL)
        self.sell_maker_ttl_profit = float(config.get("sell_maker_ttl_profit_seconds", 120))
        self.sell_maker_ttl_loss = float(config.get("sell_maker_ttl_loss_seconds", 15))
        self._bs_cost_basis: Dict[str, list] = {}          # asset_id → [total_cost, total_size]
        self._pending_sell_ids: Dict[str, str] = {}        # asset_id → unfilled maker sell order_id
        self._pending_sell_times: Dict[str, float] = {}    # asset_id → placement timestamp
        self._pending_sell_meta: Dict[str, dict] = {}      # asset_id → {shares, ttl, bs_price, slug, market_id, is_profit}
        self._ws_feed = ws_feed
        self._csv_lock = threading.Lock()
        self._seed_from_csv()

        self._credentials = {
            "apiKey": os.getenv("POLYMARKET_API_KEY"),
            "privateKey": private_key,
            "funderAddress": proxy_address,
            "signatureType": "gnosis-safe",
            "apiSecret": os.getenv("POLYMARKET_API_SECRET"),
            "passphrase": os.getenv("POLYMARKET_API_PASSPHRASE"),
        }

        logger.info("Connecting to Polymarket...")
        self.poly = pmxt.Polymarket(
            private_key=private_key,
            proxy_address=proxy_address,
            api_key=self._credentials["apiKey"],
            api_secret=self._credentials["apiSecret"],
            passphrase=self._credentials["passphrase"],
        )
        self._server_manager = _sm.ServerManager()
        logger.info("Connected.")

    def _seed_from_csv(self):
        """Re-seed _asset_copy_rate and _asset_is_low_prob from bot_trades.csv so we can
        follow exits for positions copied in previous sessions."""
        path = os.path.abspath(_BOT_TRADES_CSV)
        if not os.path.exists(path):
            return
        try:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("side") != "buy":
                        continue
                    asset_id = row.get("asset_id", "")
                    if not asset_id:
                        continue
                    self._asset_copy_rate[asset_id] = float(row.get("copy_pct", self.copy_percentage))
                    self._asset_is_low_prob[asset_id] = row.get("is_low_prob", "False").strip().lower() == "true"
            if self._asset_copy_rate:
                logger.info(f"Seeded {len(self._asset_copy_rate)} assets from bot_trades.csv")
        except Exception as e:
            logger.warning(f"Failed to seed from bot_trades.csv: {e}")

    def _log_gtc_cancelled(self, asset_id: str, placed_at: float, reason: str):
        meta = self._pending_order_meta.get(asset_id, {})
        row = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "asset_id": asset_id,
            "title": meta.get("title", ""),
            "limit_price": meta.get("limit_price", ""),
            "placed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(placed_at)),
            "cancelled_after_min": round((time.time() - placed_at) / 60, 1),
            "reason": reason,
        }
        path = os.path.abspath(_GTC_CANCELLED_CSV)
        with self._csv_lock:
            write_header = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_GTC_CANCELLED_FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)

    def _log_bot_trade(self, side: str, trade_change: Dict[str, Any],
                       our_shares: float, our_cost: float,
                       effective_copy_pct: float, is_low_prob: bool,
                       order_id: str):
        row = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "side": side,
            "title": trade_change.get("title", ""),
            "outcome": trade_change.get("outcome", ""),
            "asset_id": trade_change.get("asset", ""),
            "condition_id": trade_change.get("conditionId", ""),
            "our_shares": round(our_shares, 6),
            "price": trade_change.get("price", ""),
            "our_cost": round(our_cost, 6),
            "copy_pct": effective_copy_pct,
            "is_low_prob": is_low_prob,
            "order_id": order_id,
        }
        path = os.path.abspath(_BOT_TRADES_CSV)
        with self._csv_lock:
            write_header = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_BOT_TRADES_FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)

    def _log_copy_decision(self, trade_change: Dict[str, Any], action: str,
                            our_price=None, our_size=None, our_cost=None,
                            order_id: str = ""):
        bs_price = trade_change.get("price")
        bs_size = trade_change.get("size")
        bs_usd = (float(bs_price) * float(bs_size)) if (bs_price is not None and bs_size is not None) else ""
        row = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "action": action,
            "title": trade_change.get("title", ""),
            "outcome": trade_change.get("outcome", ""),
            "condition_id": trade_change.get("conditionId", ""),
            "asset_id": trade_change.get("asset", ""),
            "bs_price": bs_price if bs_price is not None else "",
            "bs_size": bs_size if bs_size is not None else "",
            "bs_usd": round(bs_usd, 6) if isinstance(bs_usd, float) else bs_usd,
            "our_price": our_price if our_price is not None else "",
            "our_size": our_size if our_size is not None else "",
            "our_cost": our_cost if our_cost is not None else "",
            "order_id": order_id,
        }
        path = os.path.abspath(_COPY_DECISIONS_CSV)
        try:
            with self._csv_lock:
                write_header = not os.path.exists(path)
                with open(path, "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=_COPY_DECISIONS_FIELDS)
                    if write_header:
                        writer.writeheader()
                    writer.writerow(row)
        except Exception as e:
            logger.warning(f"Failed to write copy_decisions.csv: {e}")

    def _create_order(self, market_id: str, outcome_id: str, side: str,
                      amount: float, fee: int = 1000, order_type: str = "market",
                      price: Optional[float] = None) -> Dict[str, Any]:
        """Place an order via direct HTTP to the pmxt sidecar."""
        server_info = self._server_manager.get_server_info()
        url = f"http://localhost:{server_info.get('port', 3847)}/api/polymarket/createOrder"
        token = server_info.get("accessToken", "")
        args = {
            "marketId": market_id,
            "outcomeId": outcome_id,
            "side": side,
            "type": order_type,
            "amount": amount,
            "fee": fee,
        }
        if price is not None:
            args["price"] = price
        body = {
            "args": [args],
            "credentials": self._credentials,
        }
        resp = http_requests.post(
            url,
            json=body,
            headers={
                "Content-Type": "application/json",
                "x-pmxt-access-token": token,
            },
            timeout=15,
        )
        data = resp.json()
        if not resp.ok or not data.get("success"):
            err = data.get("error", {})
            raise RuntimeError(f"[{resp.status_code}] {err.get('message', resp.text)}")
        return data["data"]

    def _get_clob_price(self, asset_id: str, side: str = "BUY") -> Optional[float]:
        """Get current price: WS cache first, REST fallback."""
        if self._ws_feed:
            price = self._ws_feed.get_ask(asset_id) if side == "BUY" else self._ws_feed.get_bid(asset_id)
            if price:
                return price
        try:
            resp = http_requests.get(
                "https://clob.polymarket.com/price",
                params={"token_id": asset_id, "side": side},
                timeout=5,
            )
            if resp.ok:
                price = float(resp.json().get("price", 0))
                return price if price > 0 else None
        except Exception as e:
            logger.debug(f"CLOB price check failed for {asset_id[:12]}: {e}")
        return None

    def _get_market_id(self, slug: str) -> Optional[str]:
        if slug in self._market_cache:
            return self._market_cache[slug]
        if len(self._market_cache) >= _MARKET_CACHE_MAXSIZE:
            self._market_cache.pop(next(iter(self._market_cache)))
        markets = self.poly.fetch_markets(slug=slug)
        market_id = markets[0].market_id if markets else None
        self._market_cache[slug] = market_id
        return market_id

    def _refresh_exposure(self):
        """Rebuild _asset_exposure, _asset_shares, and _low_prob_exposure from on-chain positions."""
        try:
            all_positions = get_user_positions(self._proxy_address)
            if all_positions is None:
                logger.warning("Failed to refresh exposure: could not fetch positions")
                return

            new_exposure = {}
            new_shares = {}
            new_low_prob_exposure = 0.0
            for p in all_positions:
                asset_id = p.get("asset")
                size = float(p.get("size", 0))
                avg_price = float(p.get("avgPrice", 0))
                if asset_id and size > 0:
                    cost = size * avg_price
                    new_exposure[asset_id] = cost
                    new_shares[asset_id] = size
                    if avg_price < self.low_prob_price_threshold:
                        new_low_prob_exposure += cost
            self._asset_exposure = new_exposure
            self._asset_shares = new_shares
            self._low_prob_exposure = new_low_prob_exposure
            self._exposure_last_refresh = time.time()
            # Clear pending orders for assets we now actually hold
            for asset_id in list(self._pending_order_ids):
                if asset_id in new_shares:
                    self._pending_order_ids.pop(asset_id, None)
                    self._pending_order_times.pop(asset_id, None)
                    self._pending_order_shares.pop(asset_id, None)
                    self._pending_order_cost.pop(asset_id, None)

            # Merge pending-order cost into exposure so max_position cap counts
            # unfilled-but-placed orders. Prevents reconcile/dispatch loops from
            # bypassing cap while on-chain state lags behind order placement.
            for asset_id, pending_cost in self._pending_order_cost.items():
                self._asset_exposure[asset_id] = self._asset_exposure.get(asset_id, 0.0) + pending_cost

            # Cancel GTC orders that have been sitting too long
            if self.gtc_order_ttl > 0:
                now = time.time()
                for asset_id in list(self._pending_order_times):
                    if now - self._pending_order_times[asset_id] > self.gtc_order_ttl:
                        oid = self._pending_order_ids.get(asset_id)
                        if oid:
                            try:
                                self.poly.cancel_order(oid)
                                logger.info(f"Cancelled stale GTC order ({self.gtc_order_ttl/60:.0f}min TTL) — {asset_id[:12]}")
                            except Exception as e:
                                logger.warning(f"Failed to cancel stale order: {e}")
                        placed_at = self._pending_order_times[asset_id]
                        self._log_gtc_cancelled(asset_id, placed_at, "ttl_expired")
                        self._pending_order_ids.pop(asset_id, None)
                        self._pending_order_times.pop(asset_id, None)
                        self._pending_order_meta.pop(asset_id, None)
                        self._pending_order_shares.pop(asset_id, None)
                        self._pending_order_cost.pop(asset_id, None)

            logger.debug(f"Refreshed exposure: {len(new_exposure)} positions, low_prob ${new_low_prob_exposure:.2f}")
        except Exception as e:
            logger.warning(f"Failed to refresh exposure: {e}")

    def _ensure_exposure_fresh(self):
        if time.time() - self._exposure_last_refresh > 60:
            self._refresh_exposure()

    def get_pending_order_shares(self, asset_id: str) -> float:
        """Shares sitting on an unfilled GTC buy order — used by reconcile to
        compute delta (bs_size - on_chain - pending) instead of re-dispatching
        full bs_size every cycle."""
        return float(self._pending_order_shares.get(asset_id, 0.0))

    def backfill_bs_cost_basis(self, wallets):
        """One-time at startup: pull recent activity per tracked wallet and
        replay BUYs/SELLs into _bs_cost_basis. Without this, early sells after
        restart fall back to market because cost basis is empty."""
        for w in wallets:
            try:
                resp = http_requests.get(
                    f"https://data-api.polymarket.com/activity?user={w}&limit=500&offset=0",
                    timeout=10,
                )
                acts = resp.json() if resp.ok else []
            except Exception as e:
                logger.warning(f"BS cost-basis backfill failed for {w[:10]}: {e}")
                continue
            trades = [a for a in acts if a.get("type") == "TRADE"]
            trades.sort(key=lambda x: x.get("timestamp", 0))
            for t in trades:
                self.update_bs_cost_basis({
                    "asset": t.get("asset"),
                    "size": t.get("size"),
                    "price": t.get("price"),
                    "type": t.get("side", "").lower(),
                })
        if self._bs_cost_basis:
            logger.info(f"Backfilled BS cost basis for {sum(1 for v in self._bs_cost_basis.values() if v[1] > 0)} held assets")

    def update_bs_cost_basis(self, change: Dict[str, Any]):
        """Size-weighted running avg of BS's cost basis per asset. Updated on
        every BS trade observed (BUY adds, SELL reduces proportionally)."""
        aid = change.get("asset")
        sz = float(change.get("size") or 0)
        px = float(change.get("price") or 0)
        if not aid or sz <= 0 or px <= 0:
            return
        cost, size = self._bs_cost_basis.get(aid, [0.0, 0.0])
        if (change.get("type") or "").lower() == "buy":
            cost += sz * px
            size += sz
        else:
            if size > 0:
                avg = cost / size
                cost = max(0.0, cost - sz * avg)
                size = max(0.0, size - sz)
                if size < 0.01:
                    cost, size = 0.0, 0.0
        self._bs_cost_basis[aid] = [cost, size]

    def get_bs_avg_cost(self, asset_id: str) -> Optional[float]:
        entry = self._bs_cost_basis.get(asset_id)
        if not entry or entry[1] <= 0:
            return None
        return entry[0] / entry[1]

    def check_pending_sells(self):
        """Run every poll cycle. For each pending maker sell past its TTL:
        cancel the limit, then market-sell remaining shares."""
        if not self._pending_sell_times:
            return
        now = time.time()
        for aid in list(self._pending_sell_times):
            placed_at = self._pending_sell_times[aid]
            meta = self._pending_sell_meta.get(aid, {})
            ttl = float(meta.get("ttl", 60))
            if now - placed_at <= ttl:
                continue
            oid = self._pending_sell_ids.get(aid)
            slug = meta.get("slug", aid[:12])
            try:
                if oid:
                    try:
                        self.poly.cancel_order(oid)
                    except Exception as e:
                        logger.warning(f"Sell TTL cancel failed {oid[:16]}: {e}")
                intended = float(meta.get("shares", 0))
                initial_pos = float(meta.get("initial_position", intended))
                current_size = 0.0
                try:
                    pos_map = {p.outcome_id: p for p in self.poly.fetch_positions()}
                    if aid in pos_map:
                        current_size = float(pos_map[aid].size)
                except Exception as e:
                    logger.warning(f"Sell TTL fetch_positions failed: {e}")
                already_filled = max(0.0, initial_pos - current_size)
                remaining = max(0.0, min(intended - already_filled, current_size))
                if remaining >= 0.01 and meta.get("market_id"):
                    logger.info(
                        f"Maker sell TTL {ttl:.0f}s expired ({'profit' if meta.get('is_profit') else 'loss'}) — "
                        f"market-selling {remaining:.2f} of {intended:.2f} intended "
                        f"(filled {already_filled:.2f} via maker) for {slug}"
                    )
                    try:
                        self._create_order(
                            market_id=meta["market_id"], outcome_id=aid,
                            side="sell", amount=remaining,
                            order_type="market", price=None,
                        )
                    except Exception as e:
                        logger.error(f"Sell TTL market fallback failed for {slug}: {e}")
                else:
                    logger.info(f"Maker sell TTL expired — fully filled or no remainder for {slug}")
            finally:
                self._pending_sell_ids.pop(aid, None)
                self._pending_sell_times.pop(aid, None)
                self._pending_sell_meta.pop(aid, None)

    def _get_usdc_balance(self) -> float:
        if time.time() - self._usdc_balance_refresh < 60:
            return self._usdc_balance_cached
        try:
            w3 = Web3(Web3.HTTPProvider(_RPC_URL))
            usdc = w3.eth.contract(address=_USDC_ADDRESS, abi=_USDC_ABI)
            raw = usdc.functions.balanceOf(Web3.to_checksum_address(self._proxy_address)).call()
            self._usdc_balance_cached = raw / 1e6
            self._usdc_balance_refresh = time.time()
            return self._usdc_balance_cached
        except Exception as e:
            logger.error(f"Failed to fetch USDC balance for cap check: {e}")
            return float('inf')  # fail open: don't block trades if check fails

    def execute_copy_trade(self, trade_change: Dict[str, Any]):
        """
        Executes a copy trade based on a detected change in someone else's positions.
        """
        self._ensure_exposure_fresh()
        try:
            side = trade_change['type'].lower()  # 'buy' or 'sell'
            asset_id = trade_change['asset']
            original_size = float(trade_change['size'])
            slug = trade_change.get('slug')
            raw_bs_price = trade_change.get('price')
            bs_price_unknown = not raw_bs_price or float(raw_bs_price) == 0
            price = raw_bs_price
            if bs_price_unknown:
                price = trade_change.get('detection_price')

            # Guard: when BS true price is unknown (Bug 2 stale avgPrice), refuse
            # to chase if current ask is high — entry_price_multiplier and Plan B
            # both degrade to "trust ask" in this case, leaving no real protection.
            if (side == 'buy' and bs_price_unknown
                    and self.unknown_price_max_ask > 0
                    and price is not None and float(price) >= self.unknown_price_max_ask):
                logger.info(
                    f"Skipping buy: BS price unknown and ask {float(price):.4f} "
                    f">= unknown_price_max_ask {self.unknown_price_max_ask:.4f}"
                )
                self._log_copy_decision(trade_change, "SKIPPED_UNKNOWN_PRICE")
                return

            # Filter: ignore small target trades (noise)
            if self.min_target_shares > 0 and original_size < self.min_target_shares:
                logger.info(f"Skipping trade: target size {original_size} below threshold {self.min_target_shares}.")
                if side == 'buy':
                    self._log_copy_decision(trade_change, "SKIPPED_MIN_SHARES")
                return

            # Filter: skip buys where target entered too late (price ceiling)
            if side == 'buy' and self.max_buy_price is not None and price is not None:
                if float(price) > self.max_buy_price:
                    logger.info(f"Skipping buy: price {float(price):.4f} exceeds ceiling {self.max_buy_price}.")
                    self._log_copy_decision(trade_change, "SKIPPED_MAX_PRICE")
                    return

            # Filter: skip blocked market keywords (BUY only)
            if side == 'buy':
                title = (trade_change.get('title') or '').lower()
                blocked = self.config.get('blocked_title_keywords', [])
                if any(kw.lower() in title for kw in blocked):
                    logger.info(f"Skipping buy: blocked keyword in title — {trade_change.get('title')}")
                    self._log_copy_decision(trade_change, "SKIPPED_BLOCKED")
                    return

            # Determine copy rate: sells use the rate stored at buy time to stay consistent
            if side == 'sell':
                effective_copy_pct = self._asset_copy_rate.get(asset_id, self.copy_percentage)
                is_low_prob = self._asset_is_low_prob.get(asset_id, False)
            else:
                is_low_prob = (price is not None and float(price) < self.low_prob_price_threshold)
                effective_copy_pct = self.low_prob_copy_percentage if is_low_prob else self.copy_percentage

            our_size = round(original_size * effective_copy_pct, 2)

            if our_size <= 0:
                logger.info(f"Skipping trade: calculated size {our_size} is too small.")
                if side == 'buy':
                    self._log_copy_decision(trade_change, "SKIPPED_SIZE_ZERO")
                return

            # Filter: skip if target's trade value is below minimum USD threshold
            if side == 'buy' and price is not None:
                target_cost = original_size * float(price)
                min_usd = self.low_prob_min_trade_usd if is_low_prob else self.min_trade_usd
                if min_usd > 0 and target_cost < min_usd:
                    logger.info(f"Skipping buy: target cost ${target_cost:.2f} below min_trade_usd ${min_usd:.2f}.")
                    self._log_copy_decision(trade_change, "SKIPPED_MIN_TRADE")
                    return

            # Filter: skip if our copy order is below Polymarket's $1 minimum
            if side == 'buy' and price is not None:
                our_cost = our_size * float(price)
                if our_cost < 1.0:
                    logger.info(f"Skipping buy: our order value ${our_cost:.2f} below Polymarket $1 minimum.")
                    self._log_copy_decision(trade_change, "SKIPPED_MIN_ORDER", our_size=our_size, our_cost=our_cost)
                    return

            # Per-asset cap: don't exceed max_position_usd in a single asset
            if side == 'buy' and self.max_position_usd > 0 and price is not None:
                order_cost = our_size * float(price)
                current_exposure = self._asset_exposure.get(asset_id, 0.0)
                if current_exposure + order_cost > self.max_position_usd:
                    logger.info(
                        f"Skipping buy: asset exposure ${current_exposure:.2f} + "
                        f"${order_cost:.2f} would exceed ${self.max_position_usd:.0f} cap"
                    )
                    self._log_copy_decision(trade_change, "SKIPPED_MAX_POS", our_size=our_size, our_cost=order_cost)
                    return

            # Portfolio cap: low-prob buys must not exceed X% of total portfolio
            if side == 'buy' and is_low_prob and price is not None:
                order_cost = our_size * float(price)
                usdc_balance = self._get_usdc_balance()
                total_portfolio = usdc_balance + sum(self._asset_exposure.values())
                max_low_prob = total_portfolio * self.low_prob_max_portfolio_pct
                remaining = max(0.0, max_low_prob - self._low_prob_exposure)
                if remaining < 1.0:
                    logger.info(
                        f"Skipping low_prob buy: cap headroom ${remaining:.2f} below $1 minimum "
                        f"({self.low_prob_max_portfolio_pct*100:.0f}% cap = ${max_low_prob:.2f} of ${total_portfolio:.2f} portfolio)"
                    )
                    self._log_copy_decision(trade_change, "SKIPPED_LOW_PROB_CAP")
                    return
                if order_cost > remaining:
                    reduced_size = round(remaining / float(price), 2)
                    if reduced_size * float(price) < 1.0:
                        logger.info(
                            f"Skipping low_prob buy: reduced size ${reduced_size * float(price):.2f} below $1 minimum."
                        )
                        self._log_copy_decision(trade_change, "SKIPPED_LOW_PROB_CAP")
                        return
                    logger.info(
                        f"Reducing low_prob buy from {our_size:.2f} to {reduced_size:.2f} shares "
                        f"(${order_cost:.2f} → ${remaining:.2f} to fit cap)"
                    )
                    our_size = reduced_size

            limit_price = None
            if side == 'buy':
                limit_price = trade_change.get('detection_price') or self._get_clob_price(asset_id, "BUY")
                # Gap guard: if ask moved >threshold away from BS's fill price,
                # cap our limit at (bs_price * threshold) instead of sweeping the book.
                # Market orders on thin books fill at top of ask stack — see NYC 54-55
                # $2.7k and NYC 60-61 over-exposure incidents.
                bs_price = trade_change.get('price')
                if (limit_price and bs_price and float(bs_price) > 0
                        and self.market_order_gap_threshold > 0):
                    gap = limit_price / float(bs_price)
                    if gap > self.market_order_gap_threshold:
                        capped = round(float(bs_price) * self.market_order_gap_threshold, 4)
                        logger.info(
                            f"Gap {(gap-1)*100:.1f}% from BS @ {float(bs_price):.4f} "
                            f"(ask {limit_price:.4f}) — capping limit at {capped:.4f} "
                            f"(no market-order sweep)"
                        )
                        limit_price = capped
                if limit_price and self.buy_limit_slip_pct > 0:
                    slipped = round(float(limit_price) * (1 + self.buy_limit_slip_pct), 4)
                    if slipped != limit_price:
                        logger.info(
                            f"Limit slip {limit_price:.4f}→{slipped:.4f} "
                            f"(+{self.buy_limit_slip_pct*100:.1f}%)"
                        )
                        limit_price = slipped
                if limit_price and bs_price and float(bs_price) > 0 and self.entry_price_multiplier > 0:
                    price_cap = round(min(float(bs_price) * self.entry_price_multiplier, 0.99), 4)
                    if limit_price > price_cap:
                        logger.info(
                            f"Capping limit {limit_price:.4f}→{price_cap:.4f} "
                            f"({self.entry_price_multiplier}x BeefSlayer @ {float(bs_price):.4f})"
                        )
                        limit_price = price_cap
                if is_low_prob and limit_price and self.low_prob_max_order_usd > 0:
                    max_shares = self.low_prob_max_order_usd / limit_price
                    if our_size > max_shares:
                        logger.info(
                            f"Capping low_prob buy from {our_size:.2f} to {max_shares:.2f} shares "
                            f"(${our_size * limit_price:.2f} → ${self.low_prob_max_order_usd:.2f} cap)"
                        )
                        our_size = round(max_shares, 2)
            else:
                current_bid = self._get_clob_price(asset_id, "SELL")
                if current_bid is not None and current_bid < 0.005:
                    logger.info(f"Skipping sell: bid too low (${current_bid:.4f}) — {slug}")
                    return

            rate_str = f" [low_prob {effective_copy_pct*100:.1f}%]" if is_low_prob else f" [{effective_copy_pct*100:.1f}%]"
            if limit_price:
                cost_str = f" limit@{limit_price:.4f} (~${our_size * limit_price:.2f})"
            elif price is not None:
                cost_str = f" market (~${our_size * float(price):.2f} est.)"
            else:
                cost_str = " market"
            logger.info(f"Copying {side} for {slug}: {our_size} shares{cost_str}{rate_str}")

            if not self.config.get("trading_enabled", False):
                logger.info("Trading disabled in config. Dry run only.")
                if side == 'buy':
                    self._log_copy_decision(trade_change, "DRY_RUN",
                                            our_price=limit_price, our_size=our_size,
                                            our_cost=(our_size * float(limit_price)) if limit_price else None)
                return

            # For sells: only proceed if we actually hold this position AND copied the buy this session
            if side == 'sell':
                if asset_id not in self._asset_copy_rate:
                    logger.info(f"Skipping sell: position not tracked this session — {slug}")
                    return
                our_positions = {p.outcome_id: p for p in self.poly.fetch_positions()}
                if asset_id not in our_positions:
                    pending_oid = self._pending_order_ids.get(asset_id)
                    if pending_oid:
                        logger.info(f"Cancelling pending buy order (BeefSlayer exiting) — {slug}")
                        placed_at = self._pending_order_times.get(asset_id, time.time())
                        try:
                            self.poly.cancel_order(pending_oid)
                            self._log_gtc_cancelled(asset_id, placed_at, "bs_exit")
                            self._pending_order_ids.pop(asset_id, None)
                            self._pending_order_times.pop(asset_id, None)
                            self._pending_order_shares.pop(asset_id, None)
                            self._pending_order_cost.pop(asset_id, None)
                            self._pending_order_meta.pop(asset_id, None)
                        except Exception as e:
                            logger.warning(f"Failed to cancel pending order {pending_oid[:16]}: {e}")
                    else:
                        logger.info(f"Skipping sell: we don't hold {asset_id[:12]}... ({slug})")
                    return
                our_size = min(our_size, float(our_positions[asset_id].size))

            market_id = trade_change.get('conditionId') or self._get_market_id(slug)
            if not market_id:
                logger.warning(f"Market not found for slug: {slug}")
                return

            # SELL: maker-with-TTL if we have BS cost basis. Skips the market sweep
            # that historically cost us ~50% slippage vs BS. Fallback to market on
            # TTL expiry or missing cost basis.
            if side == 'sell' and asset_id not in self._pending_sell_ids:
                bs_sell_price = float(trade_change.get('price') or 0)
                bs_avg = self.get_bs_avg_cost(asset_id)
                if bs_avg is not None and bs_sell_price > 0:
                    is_profit = bs_sell_price > bs_avg
                    ttl = self.sell_maker_ttl_profit if is_profit else self.sell_maker_ttl_loss
                    maker_limit = round(bs_sell_price, 4)
                    try:
                        order = self._create_order(
                            market_id=market_id, outcome_id=asset_id, side='sell',
                            amount=our_size, order_type='limit', price=maker_limit,
                        )
                    except Exception as e:
                        logger.warning(f"Maker sell failed, falling back to market: {e}")
                    else:
                        oid = order.get('id', '')
                        self._pending_sell_ids[asset_id] = oid
                        self._pending_sell_times[asset_id] = time.time()
                        self._pending_sell_meta[asset_id] = {
                            "shares": our_size, "ttl": ttl, "bs_price": bs_sell_price,
                            "slug": slug, "market_id": market_id,
                            "is_profit": is_profit, "cost_basis": bs_avg,
                            "initial_position": float(our_positions[asset_id].size),
                        }
                        logger.info(
                            f"Maker sell @ ${maker_limit:.4f} "
                            f"({'profit' if is_profit else 'loss'} exit, "
                            f"BS cost ${bs_avg:.4f}, TTL {ttl:.0f}s) — {slug}"
                        )
                        self._log_bot_trade(side, trade_change, our_size,
                                             our_size * maker_limit,
                                             effective_copy_pct, is_low_prob, oid)
                        return order

            if limit_price:
                otype = "limit"
            else:
                otype = "market"
            order = self._create_order(
                market_id=market_id,
                outcome_id=asset_id,
                side=side,
                amount=our_size,
                order_type=otype,
                price=limit_price,
            )

            # Try to extract actual fill cost from order response
            fill_cost = None
            fills = order.get("fills") or []
            if fills:
                fill_cost = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
            logger.debug(f"Order response: {order}")

            if fill_cost:
                logger.info(f"Success! Order {order['id']} — actual fill ${fill_cost:.2f}")
            else:
                logger.info(f"Success! Order {order['id']}")

            our_cost = fill_cost if fill_cost else (our_size * float(price) if price is not None else 0.0)
            self._log_bot_trade(side, trade_change, our_size, our_cost,
                                effective_copy_pct, is_low_prob, order['id'])
            if side == 'buy':
                self._log_copy_decision(trade_change, "COPIED",
                                         our_price=limit_price, our_size=our_size,
                                         our_cost=our_cost, order_id=order.get('id', ''))

            # Update exposure tracking
            if side == 'buy':
                self._asset_copy_rate[asset_id] = effective_copy_pct
                self._asset_is_low_prob[asset_id] = is_low_prob
                if otype == 'limit':
                    self._pending_order_ids[asset_id] = order['id']
                    self._pending_order_times[asset_id] = time.time()
                    self._pending_order_meta[asset_id] = {
                        "limit_price": limit_price,
                        "title": trade_change.get("title", ""),
                    }
                    self._pending_order_shares[asset_id] = our_size
                    self._pending_order_cost[asset_id] = our_size * float(limit_price) if limit_price else 0.0
                if price is not None:
                    self._asset_exposure[asset_id] = self._asset_exposure.get(asset_id, 0.0) + our_size * float(price)
                self._asset_shares[asset_id] = self._asset_shares.get(asset_id, 0.0) + our_size
                if is_low_prob and price is not None:
                    self._low_prob_exposure += our_size * float(price)
            elif side == 'sell':
                shares_held = self._asset_shares.get(asset_id, 0.0)
                if shares_held > 0:
                    fraction_sold = min(our_size / shares_held, 1.0)
                    self._asset_exposure[asset_id] = self._asset_exposure.get(asset_id, 0.0) * (1.0 - fraction_sold)
                    self._asset_shares[asset_id] = max(0.0, shares_held - our_size)
                if self._asset_shares.get(asset_id, 0.0) < 0.01:
                    self._asset_exposure.pop(asset_id, None)
                    self._asset_shares.pop(asset_id, None)
                    self._asset_copy_rate.pop(asset_id, None)
                    self._asset_is_low_prob.pop(asset_id, None)
                if is_low_prob and price is not None:
                    self._low_prob_exposure = max(0.0, self._low_prob_exposure - our_size * float(price))

            return order

        except Exception as e:
            logger.error(f"Failed to execute copy trade: {e}")
            try:
                if trade_change.get('type', '').lower() == 'buy':
                    self._log_copy_decision(trade_change, "FAILED")
            except Exception:
                pass

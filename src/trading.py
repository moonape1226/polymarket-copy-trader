import csv
import logging
import os
import json
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
        self.min_target_shares = float(config.get("min_target_shares", 0))
        self.max_buy_price = config.get("max_buy_price")
        self.min_trade_usd = float(config.get("min_trade_usd", 0))
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
        self._ws_feed = ws_feed

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
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_BOT_TRADES_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

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
            logger.debug(f"Refreshed exposure: {len(new_exposure)} positions, low_prob ${new_low_prob_exposure:.2f}")
        except Exception as e:
            logger.warning(f"Failed to refresh exposure: {e}")

    def _ensure_exposure_fresh(self):
        if time.time() - self._exposure_last_refresh > 60:
            self._refresh_exposure()

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
            price = trade_change.get('price')

            # Filter: ignore small target trades (noise)
            if self.min_target_shares > 0 and original_size < self.min_target_shares:
                logger.info(f"Skipping trade: target size {original_size} below threshold {self.min_target_shares}.")
                return

            # Filter: skip buys where target entered too late (price ceiling)
            if side == 'buy' and self.max_buy_price is not None and price is not None:
                if float(price) > self.max_buy_price:
                    logger.info(f"Skipping buy: price {float(price):.4f} exceeds ceiling {self.max_buy_price}.")
                    return

            # Filter: skip blocked market keywords (BUY only)
            if side == 'buy':
                title = (trade_change.get('title') or '').lower()
                blocked = self.config.get('blocked_title_keywords', [])
                if any(kw.lower() in title for kw in blocked):
                    logger.info(f"Skipping buy: blocked keyword in title — {trade_change.get('title')}")
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
                return

            # Filter: skip if target's trade value is below minimum USD threshold
            if side == 'buy' and price is not None:
                target_cost = original_size * float(price)
                min_usd = self.low_prob_min_trade_usd if is_low_prob else self.min_trade_usd
                if min_usd > 0 and target_cost < min_usd:
                    logger.info(f"Skipping buy: target cost ${target_cost:.2f} below min_trade_usd ${min_usd:.2f}.")
                    return

            # Filter: skip if our copy order is below Polymarket's $1 minimum
            if side == 'buy' and price is not None:
                our_cost = our_size * float(price)
                if our_cost < 1.0:
                    logger.info(f"Skipping buy: our order value ${our_cost:.2f} below Polymarket $1 minimum.")
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
                    return
                if order_cost > remaining:
                    reduced_size = round(remaining / float(price), 2)
                    if reduced_size * float(price) < 1.0:
                        logger.info(
                            f"Skipping low_prob buy: reduced size ${reduced_size * float(price):.2f} below $1 minimum."
                        )
                        return
                    logger.info(
                        f"Reducing low_prob buy from {our_size:.2f} to {reduced_size:.2f} shares "
                        f"(${order_cost:.2f} → ${remaining:.2f} to fit cap)"
                    )
                    our_size = reduced_size

            # For buys: determine limit price from WS detection or current CLOB
            buy_limit_price = None
            if side == 'buy':
                buy_limit_price = trade_change.get('detection_price') or self._get_clob_price(asset_id, "BUY")

            # Slippage guard: skip if market moved too far from detection price
            max_slippage = self.config.get("max_slippage_pct")
            detection_price = trade_change.get('detection_price')
            if side == 'buy' and max_slippage is not None and detection_price is not None:
                current_price = self._get_clob_price(asset_id, "BUY")
                if current_price is not None:
                    slippage = (current_price - detection_price) / detection_price
                    if slippage > max_slippage:
                        logger.info(
                            f"Skipping buy: slippage {slippage*100:.0f}% "
                            f"({detection_price:.4f} → {current_price:.4f}), "
                            f"exceeds {max_slippage*100:.0f}% limit — {slug}"
                        )
                        return

            rate_str = f" [low_prob {effective_copy_pct*100:.1f}%]" if is_low_prob else f" [{effective_copy_pct*100:.1f}%]"
            if buy_limit_price:
                cost_str = f" limit@{buy_limit_price:.4f} (~${our_size * buy_limit_price:.2f})"
            elif price is not None:
                cost_str = f" market (~${our_size * float(price):.2f} est.)"
            else:
                cost_str = " market"
            logger.info(f"Copying {side} for {slug}: {our_size} shares{cost_str}{rate_str}")

            if not self.config.get("trading_enabled", False):
                logger.info("Trading disabled in config. Dry run only.")
                return

            # For sells: only proceed if we actually hold this position
            if side == 'sell':
                our_positions = {p.outcome_id: p for p in self.poly.fetch_positions()}
                if asset_id not in our_positions:
                    logger.info(f"Skipping sell: we don't hold {asset_id[:12]}... ({slug})")
                    return
                our_size = min(our_size, float(our_positions[asset_id].size))

            market_id = trade_change.get('conditionId') or self._get_market_id(slug)
            if not market_id:
                logger.warning(f"Market not found for slug: {slug}")
                return

            order = self._create_order(
                market_id=market_id,
                outcome_id=asset_id,
                side=side,
                amount=our_size,
                order_type="limit" if buy_limit_price else "market",
                price=buy_limit_price,
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

            # Update exposure tracking
            if side == 'buy':
                self._asset_copy_rate[asset_id] = effective_copy_pct
                self._asset_is_low_prob[asset_id] = is_low_prob
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

import logging
import os
import json
import requests as http_requests
import pmxt
import pmxt.server_manager as _sm
from dotenv import load_dotenv
from typing import Dict, Any, Optional

load_dotenv()

logger = logging.getLogger(__name__)


_MARKET_CACHE_MAXSIZE = 1000


class TradingModule:
    def __init__(self, config: Dict[str, Any]):
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        if not private_key:
            raise EnvironmentError("POLYMARKET_PRIVATE_KEY is not set")
        if not proxy_address:
            raise EnvironmentError("POLYMARKET_PROXY_ADDRESS is not set")

        self.config = config
        self.copy_percentage = max(0.0, min(float(config.get("copy_percentage", 1.0)), 1.0))
        self.min_target_shares = float(config.get("min_target_shares", 0))
        self.max_buy_price = config.get("max_buy_price")
        self._market_cache: Dict[str, Optional[str]] = {}

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

    def _sidecar_url(self) -> str:
        port = self._server_manager.get_server_info().get("port", 3847)
        return f"http://localhost:{port}"

    def _sidecar_token(self) -> str:
        return self._server_manager.get_server_info().get("accessToken", "")

    def _create_order(self, market_id: str, outcome_id: str, side: str,
                      amount: float, fee: int = 1000) -> Dict[str, Any]:
        """Place an order via direct HTTP to the pmxt sidecar."""
        body = {
            "args": [{
                "marketId": market_id,
                "outcomeId": outcome_id,
                "side": side,
                "type": "market",
                "amount": amount,
                "fee": fee,
            }],
            "credentials": self._credentials,
        }
        resp = http_requests.post(
            f"{self._sidecar_url()}/api/polymarket/createOrder",
            json=body,
            headers={
                "Content-Type": "application/json",
                "x-pmxt-access-token": self._sidecar_token(),
            },
            timeout=15,
        )
        data = resp.json()
        if not resp.ok or not data.get("success"):
            err = data.get("error", {})
            raise RuntimeError(f"[{resp.status_code}] {err.get('message', resp.text)}")
        return data["data"]

    def _get_market_id(self, slug: str) -> Optional[str]:
        if slug in self._market_cache:
            return self._market_cache[slug]
        if len(self._market_cache) >= _MARKET_CACHE_MAXSIZE:
            self._market_cache.pop(next(iter(self._market_cache)))
        markets = self.poly.fetch_markets(slug=slug)
        market_id = markets[0].market_id if markets else None
        self._market_cache[slug] = market_id
        return market_id

    def execute_copy_trade(self, trade_change: Dict[str, Any]):
        """
        Executes a copy trade based on a detected change in someone else's positions.
        """
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

            our_size = round(original_size * self.copy_percentage, 2)

            if our_size <= 0:
                logger.info(f"Skipping trade: calculated size {our_size} is too small.")
                return

            cost_str = f" at ${float(price):.4f} (~${our_size * float(price):.2f})" if price is not None else ""
            logger.info(f"Copying {side} for {slug}: {our_size} shares{cost_str}")

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
            )

            logger.info(f"Success! Order ID: {order['id']}")
            return order

        except Exception as e:
            logger.error(f"Failed to execute copy trade: {e}")

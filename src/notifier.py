"""
Sends hourly portfolio summaries to a Slack webhook.
"""

import logging
import requests
from typing import List, Dict, Any
from web3 import Web3

from src.positions import get_user_positions

logger = logging.getLogger(__name__)

_RPC_URL     = "https://polygon-bor-rpc.publicnode.com"
_USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
_USDC_ABI    = [{"name": "balanceOf", "type": "function",
                 "inputs": [{"name": "account", "type": "address"}],
                 "outputs": [{"type": "uint256"}], "stateMutability": "view"}]


def _fetch_usdc_balance(proxy_address: str) -> float:
    try:
        w3 = Web3(Web3.HTTPProvider(_RPC_URL))
        usdc = w3.eth.contract(address=_USDC_ADDRESS, abi=_USDC_ABI)
        raw = usdc.functions.balanceOf(Web3.to_checksum_address(proxy_address)).call()
        return raw / 1e6  # USDC has 6 decimals
    except Exception as e:
        logger.error(f"Failed to fetch USDC balance: {e}")
        return 0.0


def send_portfolio_update(proxy_address: str, webhook_url: str) -> bool:
    """Fetch current portfolio and post a summary to Slack. Returns True on success."""
    positions = get_user_positions(proxy_address)
    if not positions:
        return False

    usdc_cash      = _fetch_usdc_balance(proxy_address)
    total_value    = sum(float(p.get("currentValue", 0)) for p in positions) + usdc_cash
    total_invested = sum(float(p.get("initialValue", 0)) for p in positions)
    cash_pnl       = sum(float(p.get("cashPnl", 0)) for p in positions)
    realized_pnl   = sum(float(p.get("realizedPnl", 0)) for p in positions)
    pnl_pct        = (cash_pnl / total_invested * 100) if total_invested else 0

    active    = [p for p in positions if float(p.get("currentValue", 0)) > 0.01]
    redeemable = [p for p in positions if p.get("redeemable")]

    pnl_emoji = "📈" if cash_pnl >= 0 else "📉"

    # Top 5 positions by current value
    top = sorted(active, key=lambda p: float(p.get("currentValue", 0)), reverse=True)[:5]
    top_lines = "\n".join(
        f"  • {p['title']} ({p['outcome']})  ${float(p['currentValue']):.2f}"
        for p in top
    ) or "  (none)"

    text = (
        f"{pnl_emoji} *Polymarket Portfolio Update*\n"
        f"```\n"
        f"Current Value  : ${total_value:>10,.2f}  (cash ${usdc_cash:,.2f})\n"
        f"Unrealized P&L : ${cash_pnl:>+10,.2f}  ({pnl_pct:>+.1f}%)\n"
        f"Realized P&L   : ${realized_pnl:>+10,.2f}\n"
        f"Open Positions : {len(active)}\n"
        f"Redeemable     : {len(redeemable)}\n"
        f"```\n"
        f"*Top positions:*\n{top_lines}"
    )

    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to send Slack notification: {e}")
        return False

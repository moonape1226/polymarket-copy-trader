"""
Sends hourly portfolio summaries to a Slack webhook.
"""

import logging
import os
import requests
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def _fetch_positions(proxy_address: str) -> List[Dict[str, Any]]:
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": proxy_address, "sizeThreshold": "0.01"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Failed to fetch positions for Slack report: {e}")
        return []


def send_portfolio_update(proxy_address: str, webhook_url: str) -> bool:
    """Fetch current portfolio and post a summary to Slack. Returns True on success."""
    positions = _fetch_positions(proxy_address)
    if not positions:
        return False

    total_value    = sum(float(p.get("currentValue", 0)) for p in positions)
    total_invested = sum(float(p.get("initialValue", 0)) for p in positions)
    cash_pnl       = sum(float(p.get("cashPnl", 0)) for p in positions)
    realized_pnl   = sum(float(p.get("realizedPnl", 0)) for p in positions)
    pnl_pct        = (cash_pnl / total_invested * 100) if total_invested else 0

    active    = [p for p in positions if float(p.get("currentValue", 0)) > 0.01]
    redeemable = [p for p in positions if p.get("redeemable")]

    pnl_emoji = "📈" if cash_pnl >= 0 else "📉"
    sign      = "+" if cash_pnl >= 0 else ""

    # Top 5 positions by current value
    top = sorted(active, key=lambda p: float(p.get("currentValue", 0)), reverse=True)[:5]
    top_lines = "\n".join(
        f"  • {p['title'][:45]} ({p['outcome']})  ${float(p['currentValue']):.2f}"
        for p in top
    ) or "  (none)"

    text = (
        f"{pnl_emoji} *Polymarket Portfolio Update*\n"
        f"```\n"
        f"Current Value  : ${total_value:>10,.2f}\n"
        f"Unrealized P&L : {sign}${abs(cash_pnl):>9,.2f}  ({sign}{pnl_pct:.1f}%)\n"
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

import logging
import requests
from typing import List, Dict, Any, Optional

BASE_URL = "https://data-api.polymarket.com"

logger = logging.getLogger(__name__)


_REQUIRED_POSITION_KEYS = {"asset", "size", "avgPrice"}


def _validate_positions(data: Any) -> bool:
    if not isinstance(data, list):
        return False
    return all(isinstance(p, dict) and _REQUIRED_POSITION_KEYS.issubset(p) for p in data)


def get_user_positions(wallet_address: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetches the current positions for a given wallet address from Polymarket.
    """
    url = f"{BASE_URL}/positions"
    params = {"user": wallet_address}
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not _validate_positions(data):
            logger.error(f"Unexpected response format for {wallet_address}: {type(data)}")
            return None
        return data
    except Exception as e:
        logger.error(f"Error fetching positions for {wallet_address}: {e}")
        return None


def _position_metadata(position: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'title': position.get('title'),
        'outcome': position.get('outcome'),
        'conditionId': position.get('conditionId'),
        'slug': position.get('slug'),
    }


def detect_order_changes(
    positions_tn: List[Dict[str, Any]],
    positions_tn_plus_1: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Compares two lists of positions to detect executed orders.
    Merge operations (simultaneous decrease of both YES and NO for the same
    conditionId) are detected and excluded — they are not directional signals.
    """
    map_tn = {p['asset']: p for p in positions_tn}
    map_tn_plus_1 = {p['asset']: p for p in positions_tn_plus_1}

    all_assets = set(map_tn) | set(map_tn_plus_1)
    orders = []

    for asset in all_assets:
        p_tn = map_tn.get(asset)
        p_tn_plus_1 = map_tn_plus_1.get(asset)

        # New Position (Buy)
        if p_tn is None and p_tn_plus_1 is not None:
            orders.append({
                'asset': asset,
                'type': 'BUY',
                'size': p_tn_plus_1['size'],
                'price': p_tn_plus_1['avgPrice'],
                **_position_metadata(p_tn_plus_1),
            })

        # Position Completely Sold (Sell)
        elif p_tn is not None and p_tn_plus_1 is None:
            orders.append({
                'asset': asset,
                'type': 'SELL',
                'size': p_tn['size'],
                'price': None,
                **_position_metadata(p_tn),
            })

        # Position Size Changed
        elif p_tn is not None and p_tn_plus_1 is not None:
            size_prev = float(p_tn['size'])
            size_curr = float(p_tn_plus_1['size'])
            size_diff = size_curr - size_prev

            if abs(size_diff) < 1e-9:
                continue

            order = {'asset': asset, **_position_metadata(p_tn_plus_1)}

            if size_diff > 0:
                # Buy Order: Calculate effective price from average price changes
                exec_price = (size_curr * float(p_tn_plus_1['avgPrice']) - size_prev * float(p_tn['avgPrice'])) / size_diff
                order.update({'type': 'BUY', 'size': size_diff, 'price': exec_price})
            else:
                # Sell Order: Calculate effective price from realized PnL changes
                size_sold = -size_diff
                pnl_diff = float(p_tn_plus_1.get('realizedPnl', 0)) - float(p_tn.get('realizedPnl', 0))
                exec_price = float(p_tn['avgPrice']) + (pnl_diff / size_sold)
                order.update({'type': 'SELL', 'size': size_sold, 'price': exec_price})

            orders.append(order)

    # ── Merge detection ────────────────────────────────────────────────────────
    # A Merge shows as simultaneous SELL on both YES and NO of the same
    # conditionId. Group SELL changes by conditionId; if both outcomeIndex 0
    # and 1 decreased, it's a Merge — drop both from the result.
    sells_by_condition: Dict[str, List[Dict[str, Any]]] = {}
    for o in orders:
        if o['type'] == 'SELL' and o.get('conditionId'):
            sells_by_condition.setdefault(o['conditionId'], []).append(o)

    merged_assets: set = set()
    for cid, sells in sells_by_condition.items():
        outcome_indices = set()
        for s in sells:
            asset = s['asset']
            p = map_tn.get(asset) or map_tn_plus_1.get(asset)
            if p is not None:
                outcome_indices.add(int(p.get('outcomeIndex', -1)))
        if {0, 1}.issubset(outcome_indices):
            title = sells[0].get('title', cid[:20])
            logger.info(f"Detected Merge for '{title}' — skipping both sides")
            for s in sells:
                merged_assets.add(s['asset'])

    if merged_assets:
        orders = [o for o in orders if o['asset'] not in merged_assets]

    return orders

"""Analyze copy_decisions.csv + our/BS activity to compute actual vs counterfactual PnL by price bucket.

Reads /data/copy_decisions.csv (produced by src/trading.py) and queries
data-api.polymarket.com for TRADE/REDEEM events on both our wallet and
BeefSlayer's wallet to compute per-bucket ROI.
"""
import csv
import logging
import os
from collections import defaultdict
from pathlib import Path

import requests

BS_WALLET = "0x331bf91c132af9d921e1908ca0979363fc47193f"
BASE = "https://data-api.polymarket.com"
DECISIONS_CSV = Path("/data/copy_decisions.csv")
BUCKETS = ["<$0.10", "$0.10-0.25", "$0.25-0.50", "$0.50+"]

logger = logging.getLogger(__name__)


def fetch_activity(wallet: str, atype: str, max_offset: int = 3500):
    out = []
    offset = 0
    while offset <= max_offset:
        params = {"user": wallet, "limit": 500, "offset": offset, "type": atype}
        try:
            r = requests.get(f"{BASE}/activity", params=params, timeout=20)
        except Exception as e:
            logger.warning(f"fetch_activity {wallet[:10]}/{atype}/offset={offset}: {e}")
            break
        if r.status_code != 200:
            break
        d = r.json()
        if not d:
            break
        out.extend(d)
        if len(d) < 500:
            break
        offset += 500
    return out


def _price_bucket(p: float) -> str:
    if p < 0.10:
        return "<$0.10"
    if p < 0.25:
        return "$0.10-0.25"
    if p < 0.50:
        return "$0.25-0.50"
    return "$0.50+"


def _aggregate_by_condition(trades, redeems):
    by_cid = defaultdict(lambda: {"buy_cost": 0.0, "sell": 0.0, "redeem": 0.0})
    for t in trades:
        cid = t.get("conditionId", "")
        usd = float(t.get("usdcSize", 0))
        side = t.get("side", "")
        if side == "BUY":
            by_cid[cid]["buy_cost"] += usd
        elif side == "SELL":
            by_cid[cid]["sell"] += usd
    for r in redeems:
        cid = r.get("conditionId", "")
        by_cid[cid]["redeem"] += float(r.get("usdcSize", 0))
    return by_cid


def analyze():
    if not DECISIONS_CSV.exists():
        return None
    rows = list(csv.DictReader(DECISIONS_CSV.open()))
    if not rows:
        return None

    our_wallet = (os.getenv("POLYMARKET_PROXY_ADDRESS") or "").lower()
    if not our_wallet:
        logger.error("POLYMARKET_PROXY_ADDRESS not set — cannot compute actual PnL")
        return None

    our_agg = _aggregate_by_condition(
        fetch_activity(our_wallet, "TRADE"),
        fetch_activity(our_wallet, "REDEEM"),
    )
    bs_agg = _aggregate_by_condition(
        fetch_activity(BS_WALLET, "TRADE"),
        fetch_activity(BS_WALLET, "REDEEM"),
    )

    buckets = {b: {"copied_n": 0, "copied_cost": 0.0, "copied_pnl": 0.0,
                   "skipped_n": 0, "skipped_bs_cost": 0.0, "skipped_cf_pnl": 0.0}
               for b in BUCKETS}

    for row in rows:
        try:
            bs_price = float(row["bs_price"])
        except (ValueError, KeyError, TypeError):
            continue
        if bs_price <= 0:
            continue
        bucket = _price_bucket(bs_price)
        cid = row.get("condition_id", "")
        action = row.get("action", "")

        if action == "COPIED":
            try:
                our_cost = float(row.get("our_cost") or 0)
            except ValueError:
                our_cost = 0
            if our_cost <= 0:
                continue
            agg = our_agg.get(cid, {"buy_cost": 0, "sell": 0, "redeem": 0})
            total_buy = agg["buy_cost"]
            if total_buy > 0:
                share = min(our_cost / total_buy, 1.0)
                proceeds = (agg["sell"] + agg["redeem"]) * share
                pnl = proceeds - our_cost
            else:
                pnl = -our_cost
            buckets[bucket]["copied_cost"] += our_cost
            buckets[bucket]["copied_pnl"] += pnl
            buckets[bucket]["copied_n"] += 1

        elif action.startswith("SKIPPED") or action == "FAILED":
            try:
                bs_cost = float(row.get("bs_usd") or 0)
            except ValueError:
                bs_cost = 0
            if bs_cost <= 0:
                buckets[bucket]["skipped_n"] += 1
                continue
            agg = bs_agg.get(cid, {"buy_cost": 0, "sell": 0, "redeem": 0})
            total_bs_buy = agg["buy_cost"]
            if total_bs_buy > 0:
                share = min(bs_cost / total_bs_buy, 1.0)
                cf_pnl = (agg["sell"] + agg["redeem"]) * share - bs_cost
                buckets[bucket]["skipped_cf_pnl"] += cf_pnl
                buckets[bucket]["skipped_bs_cost"] += bs_cost
            buckets[bucket]["skipped_n"] += 1

    return buckets


def format_slack(buckets) -> str:
    lines = ["*Copy-trader tier analysis — ~4 weeks of data*", "```"]
    lines.append(
        f"{'Bucket':<14} | {'Copied':>5} {'Cost':>7} {'PnL':>7} {'ROI':>7} | "
        f"{'Skip':>4} {'BS$':>6} {'CF_PnL':>7}"
    )
    for bk in BUCKETS:
        b = buckets.get(bk, {})
        cost = b.get("copied_cost", 0)
        pnl = b.get("copied_pnl", 0)
        roi = (pnl / cost * 100) if cost > 0 else 0
        lines.append(
            f"{bk:<14} | {b.get('copied_n', 0):>5} ${cost:>5.0f} ${pnl:>+5.0f} {roi:>+6.1f}% | "
            f"{b.get('skipped_n', 0):>4} ${b.get('skipped_bs_cost', 0):>4.0f} ${b.get('skipped_cf_pnl', 0):>+5.0f}"
        )
    lines.append("```")

    # Recommendation for $0.50+ bucket
    bk = buckets.get("$0.50+", {})
    cost = bk.get("copied_cost", 0)
    pnl = bk.get("copied_pnl", 0)
    if cost > 0:
        roi = pnl / cost * 100
        if roi < -1:
            lines.append(f"⚠️  $0.50+ ROI {roi:+.1f}% — consider implementing tier skip")
        elif roi < 2:
            lines.append(f"⚠️  $0.50+ ROI {roi:+.1f}% — marginal; tier skip may help")
        else:
            lines.append(f"✅ $0.50+ ROI {roi:+.1f}% — keep 100% copy")
    else:
        lines.append("ℹ️  No $0.50+ copies in window — insufficient data")
    return "\n".join(lines)


def run() -> str:
    buckets = analyze()
    if buckets is None:
        return ""
    return format_slack(buckets)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = run()
    print(out or "No data to analyze")

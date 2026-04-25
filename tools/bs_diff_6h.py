#!/usr/bin/env python3
"""
BS vs us divergence check. Pulls activity + positions for both wallets,
runs anomaly detectors, prints a report (and optionally posts to Slack).

Usage:
  python3 tools/bs_diff_6h.py --hours 6
  python3 tools/bs_diff_6h.py --hours 6 --slack
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).resolve().parent.parent
ENV = REPO / ".env"
CFG = REPO / "config.json"
API = "https://data-api.polymarket.com"

ANOMALY_MIN_USD = 5.0
OVERSELL_TOL = 1.10          # our sell shares / BS sell shares
OVERBUY_TOL = 1.15           # our buy shares / (BS buy * copy_pct)
POSITION_DIVERGE_PCT = 0.20  # 20% delta relative to expected copy size
POSITION_MIN_SHARES = 10.0
CASH_DRIFT_USD = 20.0
POSITION_MIN_MARKET_VALUE = 20.0   # ignore BS positions below $20 market value
OUR_HISTORY_DAYS = 7               # only flag divergence if we've touched this asset recently


def load_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def fetch_activity(wallet: str, since_ts: float) -> list[dict]:
    # API returns most-recent first; paginate until we're past the cutoff.
    rows: list[dict] = []
    offset = 0
    limit = 500
    while True:
        r = requests.get(
            f"{API}/activity",
            params={"user": wallet, "limit": limit, "offset": offset},
            timeout=15,
        )
        r.raise_for_status()
        batch = r.json() or []
        if not batch:
            break
        rows.extend(batch)
        if batch[-1].get("timestamp", 0) < since_ts:
            break
        offset += limit
        if offset > 5000:
            break
    return [r for r in rows if r.get("timestamp", 0) >= since_ts and r.get("type") == "TRADE"]


def fetch_positions(wallet: str) -> list[dict]:
    r = requests.get(
        f"{API}/positions",
        params={"user": wallet, "sizeThreshold": 0.01},
        timeout=15,
    )
    r.raise_for_status()
    return r.json() or []


def agg_trades_by_asset(trades: list[dict]) -> dict[str, dict]:
    """Returns {asset_id: {title, buy_sh, buy_usd, sell_sh, sell_usd}}."""
    out: dict[str, dict] = defaultdict(lambda: {"title": "", "buy_sh": 0.0, "buy_usd": 0.0, "sell_sh": 0.0, "sell_usd": 0.0})
    for t in trades:
        aid = t.get("asset")
        if not aid:
            continue
        rec = out[aid]
        rec["title"] = t.get("title") or rec["title"]
        side = (t.get("side") or "").upper()
        sz = float(t.get("size") or 0)
        usd = float(t.get("usdcSize") or 0)
        if side == "BUY":
            rec["buy_sh"] += sz
            rec["buy_usd"] += usd
        elif side == "SELL":
            rec["sell_sh"] += sz
            rec["sell_usd"] += usd
    return out


def pos_by_asset(positions: list[dict]) -> dict[str, dict]:
    return {p["asset"]: p for p in positions if p.get("asset")}


def check(hours: int, cfg: dict, bs_addr: str, our_addr: str) -> dict:
    since = time.time() - hours * 3600
    hist_since = time.time() - OUR_HISTORY_DAYS * 86400
    bs_trades = fetch_activity(bs_addr, since)
    our_trades = fetch_activity(our_addr, since)
    our_hist_trades = fetch_activity(our_addr, hist_since)
    our_recent_assets = {t.get("asset") for t in our_hist_trades if t.get("asset")}
    bs_pos = fetch_positions(bs_addr)
    our_pos = fetch_positions(our_addr)

    bs_agg = agg_trades_by_asset(bs_trades)
    our_agg = agg_trades_by_asset(our_trades)
    bs_pmap = pos_by_asset(bs_pos)
    our_pmap = pos_by_asset(our_pos)

    copy_pct = float(cfg.get("copy_percentage", 1.0))
    slip = float(cfg.get("buy_limit_slip_pct", 0.0))
    slip_low_prob = float(cfg.get("buy_limit_slip_pct_low_prob", slip))
    low_prob_threshold = float(cfg.get("low_prob_price_threshold", 0.0))

    anomalies: list[dict] = []
    all_assets = set(bs_agg) | set(our_agg) | set(bs_pmap) | set(our_pmap)

    for aid in all_assets:
        bs_t = bs_agg.get(aid, {"buy_sh": 0.0, "buy_usd": 0.0, "sell_sh": 0.0, "sell_usd": 0.0})
        our_t = our_agg.get(aid, {"buy_sh": 0.0, "buy_usd": 0.0, "sell_sh": 0.0, "sell_usd": 0.0})
        bs_p = bs_pmap.get(aid, {})
        our_p = our_pmap.get(aid, {})
        title = (bs_t.get("title") or our_t.get("title")
                 or bs_p.get("title") or our_p.get("title") or aid[:12])

        # 1. Missed trade: BS traded ≥ threshold and we did nothing in window
        bs_vol_usd = bs_t["buy_usd"] + bs_t["sell_usd"]
        our_vol_usd = our_t["buy_usd"] + our_t["sell_usd"]
        if bs_vol_usd * copy_pct >= ANOMALY_MIN_USD and our_vol_usd == 0:
            anomalies.append({
                "kind": "missed_trade", "aid": aid, "title": title,
                "detail": f"BS ${bs_vol_usd:.2f} vol, we had 0 trades",
            })

        # 2. Over-sell
        if bs_t["sell_sh"] > 0 and our_t["sell_sh"] > bs_t["sell_sh"] * OVERSELL_TOL:
            anomalies.append({
                "kind": "over_sell", "aid": aid, "title": title,
                "detail": f"BS sold {bs_t['sell_sh']:.2f}, we sold {our_t['sell_sh']:.2f}",
            })
        elif bs_t["sell_sh"] == 0 and our_t["sell_sh"] > 0:
            anomalies.append({
                "kind": "phantom_sell", "aid": aid, "title": title,
                "detail": f"we sold {our_t['sell_sh']:.2f}, BS no sells",
            })

        # 3. Over-buy vs copy_pct
        expected_buy = bs_t["buy_sh"] * copy_pct
        if expected_buy > 0 and our_t["buy_sh"] > expected_buy * OVERBUY_TOL:
            anomalies.append({
                "kind": "over_buy", "aid": aid, "title": title,
                "detail": f"BS bought {bs_t['buy_sh']:.2f} × copy={copy_pct} → expect {expected_buy:.2f}, we bought {our_t['buy_sh']:.2f}",
            })

        # 4. Slippage beyond configured slip + tolerance
        if bs_t["buy_sh"] > 0 and our_t["buy_sh"] > 0:
            bs_avg = bs_t["buy_usd"] / bs_t["buy_sh"]
            our_avg = our_t["buy_usd"] / our_t["buy_sh"]
            if bs_avg > 0:
                excess = our_avg / bs_avg - 1.0
                effective_slip = slip_low_prob if bs_avg < low_prob_threshold else slip
                if excess > effective_slip + 0.05:
                    anomalies.append({
                        "kind": "slippage", "aid": aid, "title": title,
                        "detail": f"BS avg ${bs_avg:.4f} vs ours ${our_avg:.4f} (+{excess*100:.1f}%, slip cfg {effective_slip*100:.0f}%)",
                    })

        # 5. Position divergence (end-of-window). Only flag if BS holds meaningful
        # value AND we've touched this asset recently (skip BS's legacy/untouched bags).
        bs_sh = float(bs_p.get("size") or 0)
        our_sh = float(our_p.get("size") or 0)
        bs_mv = float(bs_p.get("currentValue") or 0)
        if (bs_sh >= POSITION_MIN_SHARES
                and bs_mv >= POSITION_MIN_MARKET_VALUE
                and aid in our_recent_assets):
            expected_sh = bs_sh * copy_pct
            if expected_sh > 0:
                delta_pct = abs(our_sh - expected_sh) / expected_sh
                if delta_pct > POSITION_DIVERGE_PCT:
                    anomalies.append({
                        "kind": "position_divergence", "aid": aid, "title": title,
                        "detail": f"BS holds {bs_sh:.2f} (mv ${bs_mv:.2f}), expect {expected_sh:.2f}, we hold {our_sh:.2f} (Δ{delta_pct*100:.0f}%)",
                    })

        # 6. Cash flow drift (net spend per market)
        bs_net = bs_t["buy_usd"] - bs_t["sell_usd"]
        our_net = our_t["buy_usd"] - our_t["sell_usd"]
        drift = our_net - bs_net * copy_pct
        if abs(drift) > CASH_DRIFT_USD:
            anomalies.append({
                "kind": "cash_drift", "aid": aid, "title": title,
                "detail": f"BS net ${bs_net:.2f} × copy={copy_pct} → expect ${bs_net*copy_pct:.2f}, ours ${our_net:.2f} (drift ${drift:+.2f})",
            })

    return {
        "window_hours": hours,
        "bs_trades": len(bs_trades),
        "our_trades": len(our_trades),
        "bs_positions": len(bs_pos),
        "our_positions": len(our_pos),
        "assets_checked": len(all_assets),
        "anomalies": anomalies,
    }


def format_report(r: dict) -> str:
    lines = [
        f"*BS diff {r['window_hours']}h* — BS trades:{r['bs_trades']} ours:{r['our_trades']} "
        f"assets:{r['assets_checked']} *anomalies:{len(r['anomalies'])}*",
    ]
    if not r["anomalies"]:
        lines.append("_clean_")
        return "\n".join(lines)
    by_kind = defaultdict(list)
    for a in r["anomalies"]:
        by_kind[a["kind"]].append(a)
    for kind, items in by_kind.items():
        lines.append(f"\n*{kind}* ({len(items)}):")
        for a in items[:8]:
            lines.append(f"  • {a['title'][:60]} — {a['detail']}")
        if len(items) > 8:
            lines.append(f"  … +{len(items)-8} more")
    return "\n".join(lines)


def post_slack(webhook: str, text: str) -> None:
    requests.post(webhook, json={"text": text}, timeout=10).raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--slack", action="store_true")
    ap.add_argument("--json", action="store_true", help="emit JSON result to stdout")
    args = ap.parse_args()

    env = load_env(ENV)
    cfg = json.loads(CFG.read_text())
    bs_addr = cfg["wallets_to_track"][0]
    our_addr = (env.get("POLYMARKET_PROXY_ADDRESS") or os.environ.get("POLYMARKET_PROXY_ADDRESS") or "").strip()
    if not our_addr:
        print("POLYMARKET_PROXY_ADDRESS not set", file=sys.stderr)
        return 2

    r = check(args.hours, cfg, bs_addr, our_addr)

    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(format_report(r))

    if args.slack:
        hook = env.get("SLACK_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL")
        if not hook:
            print("SLACK_WEBHOOK_URL not set; skipping Slack post", file=sys.stderr)
        else:
            post_slack(hook, format_report(r))

    return 1 if r["anomalies"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Re-settle paper_settled.csv RESOLVE rows using IEM-backed nws_actuals.csv
as the source of truth (covers 4/22-5/14, replacing NWS API which has ~7-day
retention).

Detects the obs_cache key bug from predictor.py:1189 (kind missing from cache key).
Read-only against paper_settled.csv. Writes corrected report to
data/analysis/resettle_report.csv.
"""

import csv
import math
import os
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
SETTLED = os.path.join(ROOT, "data", "paper_settled.csv")
ACTUALS = os.path.join(ROOT, "data", "nws_actuals.csv")
OUT_DIR = os.path.join(ROOT, "data", "analysis")
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "resettle_report.csv")


def in_bucket(extreme, bucket):
    # Mirror predictor settlement (A10): half-up round to the settled integer,
    # then inclusive integer bounds. Raw half-open ranges flipped boundary
    # cases (e.g. 68.0 in "67-68"/"<=68" was wrongly a loss).
    r = math.floor(extreme + 0.5)
    if bucket.startswith("<="):
        return r <= float(bucket[2:])
    if bucket.startswith(">="):
        return r >= float(bucket[2:])
    lo_str, _, hi_str = bucket.partition("-")
    return float(lo_str) <= r <= float(hi_str)


def main():
    # Build (city, local_date) -> (lo, hi) from nws_actuals
    actuals = {}
    for r in csv.DictReader(open(ACTUALS)):
        if r.get("status") != "ok":
            continue
        try:
            actuals[(r["city"], r["local_date"])] = (
                float(r["actual_low"]),
                float(r["actual_high"]),
            )
        except Exception:
            continue
    print(f"loaded actuals: {len(actuals)} (city, local_date) pairs")

    rows = list(csv.DictReader(open(SETTLED)))
    resolves = [r for r in rows if r.get("exit_kind") == "RESOLVE"]
    print(f"paper_settled total: {len(rows)}  RESOLVE: {len(resolves)}")

    out_rows = []
    no_actual = 0
    contam = 0
    flips = 0
    total_pnl_old = 0.0
    total_pnl_new = 0.0
    by_kind_old = defaultdict(lambda: [0, 0.0, 0])
    by_kind_new = defaultdict(lambda: [0, 0.0, 0])
    by_city_kind_new = defaultdict(lambda: [0, 0.0, 0])

    for r in resolves:
        key = (r["city"], r["local_date"])
        if key not in actuals:
            no_actual += 1
            continue
        lo_a, hi_a = actuals[key]
        correct_extreme = hi_a if r["kind"] == "highest" else lo_a
        old_extreme = float(r["settle_temp_f"]) if r["settle_temp_f"] else None
        cost = float(r["cost_usd"])
        shares = float(r["shares"])
        side = r["side"]
        yes_won_new = in_bucket(correct_extreme, r["bucket"])
        won_new = yes_won_new if side == "YES" else (not yes_won_new)
        payout_new = shares if won_new else 0.0
        pnl_new = payout_new - cost
        won_old = r["won"] == "1"
        pnl_old = float(r["pnl_usd"])
        contam_flag = old_extreme is not None and abs(old_extreme - correct_extreme) > 0.05
        flip = won_old != won_new
        if contam_flag:
            contam += 1
        if flip:
            flips += 1
        total_pnl_old += pnl_old
        total_pnl_new += pnl_new
        by_kind_old[r["kind"]][0] += 1
        by_kind_old[r["kind"]][1] += pnl_old
        by_kind_old[r["kind"]][2] += int(won_old)
        by_kind_new[r["kind"]][0] += 1
        by_kind_new[r["kind"]][1] += pnl_new
        by_kind_new[r["kind"]][2] += int(won_new)
        by_city_kind_new[(r["city"], r["kind"])][0] += 1
        by_city_kind_new[(r["city"], r["kind"])][1] += pnl_new
        by_city_kind_new[(r["city"], r["kind"])][2] += int(won_new)
        out_rows.append({
            "ts_settled": r["ts_settled"],
            "city": r["city"], "kind": r["kind"], "local_date": r["local_date"],
            "bucket": r["bucket"], "side": side,
            "cost_usd": f"{cost:.2f}",
            "old_actual": r["settle_temp_f"], "new_actual": f"{correct_extreme:.2f}",
            "contaminated": "1" if contam_flag else "0",
            "old_won": r["won"], "new_won": "1" if won_new else "0",
            "won_flipped": "1" if flip else "0",
            "old_pnl": f"{pnl_old:+.2f}", "new_pnl": f"{pnl_new:+.2f}",
            "pnl_delta": f"{pnl_new - pnl_old:+.2f}",
        })

    if out_rows:
        with open(OUT, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            for r in out_rows:
                w.writerow(r)

    print()
    print(f"== resettle summary ==")
    print(f"RESOLVE rows resettled: {len(out_rows)}  (no IEM actual: {no_actual})")
    print(f"contaminated (old_actual != new_actual): {contam}")
    print(f"won flipped: {flips}")
    print(f"RESOLVE PnL  old: ${total_pnl_old:+.2f}   new: ${total_pnl_new:+.2f}   delta: ${total_pnl_new-total_pnl_old:+.2f}")
    print()
    print("by kind (RESOLVE only, resettled):")
    for k in sorted(by_kind_old):
        no, po, wo = by_kind_old[k]
        nn, pn, wn = by_kind_new[k]
        print(f"  {k:8s}  old: n={no:3d} win={wo:3d} pnl=${po:+8.2f}   →   new: n={nn:3d} win={wn:3d} pnl=${pn:+8.2f}")

    print()
    print("by city × kind (new, RESOLVE only):")
    for (c, k), (n, pnl, w) in sorted(by_city_kind_new.items(), key=lambda x: -x[1][1]):
        print(f"  {c:10s} {k:8s}  n={n:3d}  win={w:3d}  pnl=${pnl:+8.2f}")

    print()
    print(f"report -> {OUT}")


if __name__ == "__main__":
    main()

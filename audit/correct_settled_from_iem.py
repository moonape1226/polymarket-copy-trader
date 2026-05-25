"""Overwrite contaminated RESOLVE rows in paper_settled.csv with IEM-corrected
settle_temp_f / won / pnl_usd. Other fields untouched.

The 5/15 obs_cache key-contamination bug caused 74 of 93 RESOLVE rows to
record the wrong daily extreme (highest and lowest shared a cache key, so
the second kind to settle inherited the first's value). audit/resettle_lowest.py
already computed the correct values; this script makes the correction
persistent.

Backup: paper_settled.csv -> paper_settled.csv.bak (atomic; halts on conflict).
Audit trail: corrected rows logged with original vs new values.
"""

import csv
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SETTLED = os.path.join(ROOT, "data", "paper_settled.csv")
ACTUALS = os.path.join(ROOT, "data", "nws_actuals.csv")
BAK = SETTLED + ".bak"


def in_bucket(extreme, bucket):
    if bucket.startswith("<="):
        return extreme < float(bucket[2:])
    if bucket.startswith(">="):
        return extreme >= float(bucket[2:])
    lo_s, _, hi_s = bucket.partition("-")
    return float(lo_s) <= extreme < float(hi_s)


def main():
    if os.path.exists(BAK):
        sys.exit(f"refusing to clobber existing backup at {BAK}; remove it first")
    actuals = {}
    for r in csv.DictReader(open(ACTUALS)):
        if r.get("status") != "ok":
            continue
        try:
            actuals[(r["city"], r["local_date"])] = (
                float(r["actual_low"]), float(r["actual_high"])
            )
        except Exception:
            continue
    print(f"IEM actuals loaded: {len(actuals)}")

    rows = list(csv.DictReader(open(SETTLED)))
    if not rows:
        sys.exit("paper_settled.csv empty")
    fields = list(rows[0].keys())

    shutil.copy2(SETTLED, BAK)
    print(f"backup -> {BAK}")

    corrected = 0
    no_actual = 0
    unchanged = 0
    for r in rows:
        if r.get("exit_kind") != "RESOLVE":
            continue
        k = (r["city"], r["local_date"])
        if k not in actuals:
            no_actual += 1
            continue
        lo, hi = actuals[k]
        correct = hi if r["kind"] == "highest" else lo
        try:
            old_settle = float(r["settle_temp_f"]) if r.get("settle_temp_f") else None
            cost = float(r["cost_usd"])
            shares = float(r["shares"])
        except Exception:
            continue
        if old_settle is not None and abs(old_settle - correct) < 0.05:
            unchanged += 1
            continue
        yes_won = in_bucket(correct, r["bucket"])
        won_new = yes_won if r["side"] == "YES" else (not yes_won)
        pnl_new = (shares if won_new else 0.0) - cost
        old_won = r.get("won", "")
        old_pnl = r.get("pnl_usd", "")
        r["settle_temp_f"] = f"{correct:.2f}"
        r["won"] = "1" if won_new else "0"
        r["pnl_usd"] = f"{pnl_new:.2f}"
        corrected += 1
        print(f"  {r['city']:7s} {r['kind']:7s} {r['local_date']} {r['bucket']:>6s} {r['side']}  "
              f"settle {old_settle:.2f}→{correct:.2f}  won {old_won}→{r['won']}  "
              f"pnl {old_pnl}→{r['pnl_usd']}")

    tmp = SETTLED + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, SETTLED)

    print()
    print(f"== correction summary ==")
    print(f"  corrected:           {corrected}")
    print(f"  unchanged (no diff): {unchanged}")
    print(f"  no IEM actual:       {no_actual}")
    print(f"  written -> {SETTLED}")
    print(f"  rollback: mv {BAK} {SETTLED}")


if __name__ == "__main__":
    main()

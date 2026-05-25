"""Compute per-kind (bias, sigma) calibration from IEM actuals + paper_settled t_hat.

Reads:
  data/nws_actuals.csv  (IEM ground truth, post cache-bug-free)
  data/paper_settled.csv (entry t_hat per position)

Writes:
  data/calibration.json with schema:
    {
      "per_kind": {
        "highest": {"bias": float, "sigma": float, "n": int},
        "lowest":  {"bias": float, "sigma": float, "n": int}
      },
      "computed_at": ISO timestamp,
      "source": "iem-vs-paper_settled t_hat_entry"
    }

Dedups by (city, kind, local_date) — t_hat is identical across buckets for one
forecast snapshot. One residual per market-day.
"""

import csv
import json
import os
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
ACTUALS = os.path.join(ROOT, "data", "nws_actuals.csv")
SETTLED = os.path.join(ROOT, "data", "paper_settled.csv")
OUT = os.path.join(ROOT, "data", "calibration.json")


def main():
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
    print(f"actuals: {len(actuals)} (city, date) pairs")

    seen = set()
    residuals = {"highest": [], "lowest": []}
    city_residuals: dict[tuple[str, str], list[float]] = {}
    for r in csv.DictReader(open(SETTLED)):
        kind = r.get("kind")
        if kind not in residuals:
            continue
        if not r.get("t_hat_entry"):
            continue
        key = (r["city"], r["kind"], r["local_date"])
        if key in seen:
            continue
        ak = (r["city"], r["local_date"])
        if ak not in actuals:
            continue
        seen.add(key)
        try:
            t_hat = float(r["t_hat_entry"])
            lo, hi = actuals[ak]
            actual = lo if kind == "lowest" else hi
            resid = actual - t_hat
            residuals[kind].append(resid)
            city_residuals.setdefault((r["city"], kind), []).append(resid)
        except Exception:
            continue

    per_kind = {}
    for kind, arr in residuals.items():
        n = len(arr)
        if n < 5:
            print(f"  {kind}: n={n} too small, skip")
            continue
        mean = sum(arr) / n
        var = sum((x - mean) ** 2 for x in arr) / (n - 1)
        sigma = var ** 0.5
        per_kind[kind] = {"bias": round(mean, 3), "sigma": round(sigma, 3), "n": n}
        print(f"  {kind:8s}: n={n:3d}  mean={mean:+.3f}°F  σ={sigma:.3f}°F  "
              f"range=[{min(arr):+.1f}, {max(arr):+.1f}]")

    # Per-(city, kind) bias only. σ stays per-kind: per-city n is too small to
    # estimate variance reliably, but a city-level mean shift (e.g. Miami lowest
    # ~+1.6°F warm) is a strong, stable signal worth correcting. Min n=8.
    per_city_kind = {}
    for (city, kind), arr in sorted(city_residuals.items()):
        n = len(arr)
        if n < 8:
            print(f"  {city} {kind}: n={n} too small, skip (fallback per_kind)")
            continue
        mean = sum(arr) / n
        per_city_kind[f"{city}|{kind}"] = {"bias": round(mean, 3), "n": n}
        print(f"  {city:8s} {kind:8s}: n={n:3d}  bias={mean:+.3f}°F  "
              f"range=[{min(arr):+.1f}, {max(arr):+.1f}]")

    payload = {
        "per_kind": per_kind,
        "per_city_kind": per_city_kind,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": "iem-vs-paper_settled t_hat_entry",
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

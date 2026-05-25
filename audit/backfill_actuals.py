"""Backfill historical station observations for past (city, local_date) pairs.

Source: Iowa State Mesonet ASOS endpoint (raw METAR archive, free, no key,
multi-decade history). Output: data/nws_actuals.csv (same schema audit_paired
uses for the actual_high/low/coverage fields).

Idempotent. Re-running retries rows whose status is "incomplete".

Usage:
    python3 audit/backfill_actuals.py [--start 2026-04-22] [--end 2026-05-04]
                                      [--cities NYC,Miami] [--data-dir ./data]
"""

import argparse
import csv
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "data"))
os.environ.setdefault("DATA_DIR", DEFAULT_DATA_DIR)

sys.path.insert(0, SCRIPT_DIR)
from audit import (  # noqa: E402
    CITIES,
    coverage_metrics,
    day_window_local,
    observation_coverage_ok,
    observation_samples_for_local_day,
)

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
HTTP_TIMEOUT = 60
THROTTLE_SECONDS = 1.0

FIELDS = [
    "city", "station", "local_date", "tz", "fetched_at",
    "n_obs", "obs_hours_covered", "obs_max_gap_hours",
    "actual_high", "actual_low", "status",
]


def iem_station(asos_code):
    """IEM strips the leading 'K' for CONUS stations."""
    return asos_code[1:] if asos_code.startswith("K") and len(asos_code) == 4 else asos_code


def fetch_iem_range(station, start_d, end_d):
    """Fetch all valid (ts, temperature_f) for [start_d, end_d+1 day) UTC.
    Returns list of {ts: ISO UTC, temperature_f}."""
    iem_code = iem_station(station)
    params = {
        "station": iem_code,
        "data": "tmpf",
        "year1": start_d.year, "month1": start_d.month, "day1": start_d.day,
        "year2": (end_d + timedelta(days=2)).year,
        "month2": (end_d + timedelta(days=2)).month,
        "day2": (end_d + timedelta(days=2)).day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "latlon": "no",
        "missing": "empty",
        "trace": "empty",
    }
    r = requests.get(IEM_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        tmpf = (row.get("tmpf") or "").strip()
        valid = (row.get("valid") or "").strip()
        if not tmpf or not valid:
            continue
        try:
            t = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            f = float(tmpf)
        except Exception:
            continue
        out.append({"ts": t.isoformat(), "temperature_f": f})
    return out


def load_existing(path):
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            out[(r["city"], r["local_date"])] = r
    return out


def write_all(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda x: (x["local_date"], x["city"])):
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, path)


def summarize(city, info, local_date_str, all_obs, now_utc):
    day_start, day_end = day_window_local(local_date_str, info["tz"])
    samples = observation_samples_for_local_day(all_obs, day_start, day_end)
    metrics = coverage_metrics(samples, day_start, day_end)
    ok = observation_coverage_ok(samples, day_start, day_end)
    row = {
        "city": city,
        "station": info["station"],
        "local_date": local_date_str,
        "tz": info["tz"],
        "fetched_at": now_utc.isoformat(),
        "n_obs": metrics["n"],
        "obs_hours_covered": metrics["hours_covered"],
        "obs_max_gap_hours": (
            round(metrics["max_gap_hours"], 3) if metrics["max_gap_hours"] is not None else ""
        ),
        "actual_high": "",
        "actual_low": "",
        "status": "ok" if ok else "incomplete",
    }
    if ok:
        temps = [s["temperature_f"] for s in samples]
        row["actual_high"] = round(max(temps), 2)
        row["actual_low"] = round(min(temps), 2)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-04-22")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD; default: yesterday UTC")
    ap.add_argument("--cities", default=None, help="comma list; default: all")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = ap.parse_args()

    out_csv = os.path.join(args.data_dir, "nws_actuals.csv")
    end = args.end or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    cities = args.cities.split(",") if args.cities else list(CITIES)
    start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    print(f"backfill {args.start} -> {end}  cities={cities}  out={out_csv}")

    existing = load_existing(out_csv)
    print(f"existing rows: {len(existing)}  ok={sum(1 for r in existing.values() if r.get('status')=='ok')}")

    now_utc = datetime.now(timezone.utc)
    fetched = retried = skipped = too_early = 0

    for city in cities:
        info = CITIES.get(city)
        if not info:
            print(f"  skip unknown city {city}")
            continue
        # Determine which dates need work
        all_dates = []
        cur = start_d
        while cur <= end_d:
            local_date = cur.isoformat()
            day_start, day_end = day_window_local(local_date, info["tz"])
            if now_utc < day_end.astimezone(timezone.utc) + timedelta(hours=4):
                too_early += 1
            else:
                prev = existing.get((city, local_date))
                if prev and prev.get("status") == "ok":
                    skipped += 1
                else:
                    all_dates.append(local_date)
            cur += timedelta(days=1)
        if not all_dates:
            continue
        print(f"  {city} ({info['station']}): need {len(all_dates)} dates  fetching IEM...")
        try:
            obs = fetch_iem_range(info["station"], start_d, end_d)
        except Exception as e:
            print(f"  {city} fetch failed: {e}")
            continue
        print(f"    got {len(obs)} observations")
        for local_date in all_dates:
            prev = existing.get((city, local_date))
            row = summarize(city, info, local_date, obs, now_utc)
            existing[(city, local_date)] = row
            if prev:
                retried += 1
            else:
                fetched += 1
            print(
                f"    {local_date}  status={row['status']:10s} "
                f"n={row['n_obs']:3d}  hours={row['obs_hours_covered']:2d}  "
                f"gap={row['obs_max_gap_hours']}  hi={row['actual_high']}  lo={row['actual_low']}"
            )
        write_all(out_csv, existing)
        time.sleep(THROTTLE_SECONDS)

    print(
        f"done: fetched={fetched}  retried={retried}  skipped(ok)={skipped}  "
        f"too_early={too_early}  total={len(existing)}"
    )


if __name__ == "__main__":
    main()

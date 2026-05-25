"""Forecast-vs-observation audit logger.

Decoupled from trading: records all 6 forecasts (1 NWS + 5 Open-Meteo) at the
start of each city's local day, then pairs each lock with the daily extremes
observed at the same airport station after the local day closes. Used to measure systematic
forecast bias independent of which markets were traded.

Output: data/audit_paired.csv (one row per city/local_date, all forecasts +
actuals + per-source bias). State: data/audit_locks.json (pending locks).
"""

import csv
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests


NWS = "https://api.weather.gov"
NWS_HEADERS = {
    "User-Agent": "weather-audit research (paper-trader)",
    "Accept": "application/geo+json",
}
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MODELS = [
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "ukmo_global_deterministic_10km",
]
HTTP_TIMEOUT = 30

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PAIRED_CSV = os.path.join(DATA_DIR, "audit_paired.csv")
REJECTED_CSV = os.path.join(DATA_DIR, "audit_rejected.csv")
LOCKS_JSON = os.path.join(DATA_DIR, "audit_locks.json")
TICK_SECONDS = int(os.environ.get("AUDIT_TICK_SECONDS", "1800"))
REJECT_AFTER_DAYS = int(os.environ.get("AUDIT_REJECT_AFTER_DAYS", "7"))

FORECAST_MAX_GAP_HOURS = 3.0
OBS_MIN_COUNT = 18
OBS_MIN_HOURS = 20
OBS_MAX_GAP_HOURS = 3.0

# Active markets only. tz used to define each city's "day".
CITIES = {
    "Seattle":  {"lat": 47.4502, "lon": -122.3088, "station": "KSEA", "tz": "America/Los_Angeles"},
    "NYC":      {"lat": 40.7772, "lon": -73.8726,  "station": "KLGA", "tz": "America/New_York"},
    "Chicago":  {"lat": 41.9786, "lon": -87.9048,  "station": "KORD", "tz": "America/Chicago"},
    "Dallas":   {"lat": 32.8471, "lon": -96.8517,  "station": "KDAL", "tz": "America/Chicago"},
    "Atlanta":  {"lat": 33.6407, "lon": -84.4277,  "station": "KATL", "tz": "America/New_York"},
    "Miami":    {"lat": 25.7959, "lon": -80.2870,  "station": "KMIA", "tz": "America/New_York"},
    "LA":       {"lat": 33.9425, "lon": -118.4081, "station": "KLAX", "tz": "America/Los_Angeles"},
    "Houston":  {"lat": 29.6457, "lon": -95.2789,  "station": "KHOU", "tz": "America/Chicago"},
    "Denver":   {"lat": 39.7017, "lon": -104.7522, "station": "KBKF", "tz": "America/Denver"},
    "Austin":   {"lat": 30.1945, "lon": -97.6699,  "station": "KAUS", "tz": "America/Chicago"},
    "SF":       {"lat": 37.6189, "lon": -122.3750, "station": "KSFO", "tz": "America/Los_Angeles"},
}

CSV_FIELDS = [
    "locked_at", "city", "station", "local_date", "tz",
    "fcst_nws_high", "fcst_nws_low",
    "fcst_ecmwf_ifs025_high", "fcst_ecmwf_ifs025_low",
    "fcst_gfs_seamless_high", "fcst_gfs_seamless_low",
    "fcst_icon_seamless_high", "fcst_icon_seamless_low",
    "fcst_gem_seamless_high", "fcst_gem_seamless_low",
    "fcst_ukmo_global_deterministic_10km_high", "fcst_ukmo_global_deterministic_10km_low",
    "fcst_ensemble_high", "fcst_ensemble_low", "source_count",
    "finalized_at", "n_obs", "obs_hours_covered", "obs_max_gap_hours", "actual_high", "actual_low",
    "bias_nws_high", "bias_nws_low",
    "bias_ecmwf_ifs025_high", "bias_ecmwf_ifs025_low",
    "bias_gfs_seamless_high", "bias_gfs_seamless_low",
    "bias_icon_seamless_high", "bias_icon_seamless_low",
    "bias_gem_seamless_high", "bias_gem_seamless_low",
    "bias_ukmo_global_deterministic_10km_high", "bias_ukmo_global_deterministic_10km_low",
    "bias_ensemble_high", "bias_ensemble_low",
]

REJECTED_FIELDS = [
    "locked_at", "city", "station", "local_date", "tz",
    "rejected_at", "reason", "n_obs", "obs_hours_covered", "obs_max_gap_hours", "source_count",
]

SOURCE_KEYS = ["nws"] + OPEN_METEO_MODELS + ["ensemble"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("audit")


def load_locks() -> dict:
    if not os.path.exists(LOCKS_JSON):
        return {}
    try:
        with open(LOCKS_JSON) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"locks load failed, starting empty: {e}")
        return {}


def save_locks(d: dict) -> None:
    tmp = LOCKS_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, LOCKS_JSON)


def append_paired(row: dict) -> None:
    new = not os.path.exists(PAIRED_CSV)
    with open(PAIRED_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def append_rejected(row: dict) -> None:
    new = not os.path.exists(REJECTED_CSV)
    with open(REJECTED_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REJECTED_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in REJECTED_FIELDS})


def http_get_json(url, params=None, headers=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                log.warning(f"GET failed {url}: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def nws_grid_url(info):
    j = http_get_json(f"{NWS}/points/{info['lat']},{info['lon']}", headers=NWS_HEADERS)
    if not j:
        return None
    return j.get("properties", {}).get("forecastHourly")


def nws_hourly_periods(info):
    url = nws_grid_url(info)
    if not url:
        return []
    j = http_get_json(url, headers=NWS_HEADERS)
    if not j:
        return []
    out = []
    for p in j.get("properties", {}).get("periods", []) or []:
        try:
            t = p.get("temperature")
            if t is None:
                continue
            unit = p.get("temperatureUnit", "F")
            t_f = float(t) if unit == "F" else float(t) * 9 / 5 + 32
            out.append({"startTime": p["startTime"], "temperature_f": t_f})
        except Exception:
            continue
    return out


def openmeteo_hourly(info):
    j = http_get_json(
        OPEN_METEO,
        params={
            "latitude": info["lat"],
            "longitude": info["lon"],
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "models": ",".join(OPEN_METEO_MODELS),
            "forecast_days": 2,
            "timezone": info["tz"],
        },
    )
    if not j:
        return {}
    hourly = j.get("hourly", {}) or {}
    times = hourly.get("time") or []
    out = {}
    for m in OPEN_METEO_MODELS:
        temps = hourly.get(f"temperature_2m_{m}") or []
        if not temps or len(temps) != len(times):
            continue
        rows = []
        for t, x in zip(times, temps):
            if x is None:
                continue
            try:
                rows.append({"startTime": t, "temperature_f": float(x)})
            except Exception:
                continue
        if rows:
            out[m] = rows
    return out


def nws_observations(station, start_iso, end_iso):
    j = http_get_json(
        f"{NWS}/stations/{station}/observations",
        params={"start": start_iso, "end": end_iso},
        headers=NWS_HEADERS,
    )
    if not j:
        return []
    out = []
    for f in j.get("features", []) or []:
        try:
            props = f.get("properties", {})
            t = props.get("temperature", {}).get("value")  # Celsius
            ts = props.get("timestamp")
            if t is None or not ts:
                continue
            out.append({"ts": ts, "temperature_f": float(t) * 9 / 5 + 32})
        except Exception:
            continue
    return out


def day_window_local(local_date_str, tz_name):
    tz = ZoneInfo(tz_name)
    d = datetime.strptime(local_date_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def _parse_iso_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def local_day_samples(periods, day_start, day_end):
    """Periods may have UTC ISO (NWS) or naive local ISO (Open-Meteo with timezone=tz).
    Both compare correctly once converted to/treated as the city local tz."""
    tz = day_start.tzinfo
    hits = []
    for p in periods:
        try:
            t = _parse_iso_dt(p["startTime"])
        except Exception:
            continue
        t_local = t.replace(tzinfo=tz) if t.tzinfo is None else t.astimezone(tz)
        if day_start <= t_local < day_end:
            hits.append({"dt": t_local, "temperature_f": p["temperature_f"]})
    return hits


def _hour_key(dt):
    return (dt.date().isoformat(), dt.hour, getattr(dt, "fold", 0))


def coverage_metrics(samples, day_start, day_end):
    ordered = sorted(samples, key=lambda s: s["dt"].astimezone(timezone.utc))
    expected_hours = round(
        (day_end.astimezone(timezone.utc) - day_start.astimezone(timezone.utc)).total_seconds() / 3600
    )
    if not ordered:
        return {
            "n": 0,
            "hours_covered": 0,
            "max_gap_hours": None,
            "first": None,
            "last": None,
            "expected_hours": expected_hours,
        }
    utc_times = [s["dt"].astimezone(timezone.utc) for s in ordered]
    gaps = [
        (b - a).total_seconds() / 3600
        for a, b in zip(utc_times, utc_times[1:])
    ]
    return {
        "n": len(ordered),
        "hours_covered": len({_hour_key(s["dt"]) for s in ordered}),
        "max_gap_hours": max(gaps) if gaps else None,
        "first": ordered[0]["dt"],
        "last": ordered[-1]["dt"],
        "expected_hours": expected_hours,
    }


def forecast_coverage_ok(samples, day_start, day_end):
    m = coverage_metrics(samples, day_start, day_end)
    min_hours = max(int(m["expected_hours"]) - 2, 1)
    return (
        m["hours_covered"] >= min_hours
        and m["first"] is not None
        and m["last"] is not None
        and m["first"] <= day_start + timedelta(hours=2)
        and m["last"] >= day_start + timedelta(hours=22)
        and m["max_gap_hours"] is not None
        and m["max_gap_hours"] <= FORECAST_MAX_GAP_HOURS
    )


def observation_coverage_ok(samples, day_start, day_end):
    m = coverage_metrics(samples, day_start, day_end)
    return (
        m["n"] >= OBS_MIN_COUNT
        and m["hours_covered"] >= OBS_MIN_HOURS
        and m["first"] is not None
        and m["last"] is not None
        and m["first"] <= day_start + timedelta(hours=2)
        and m["last"] >= day_start + timedelta(hours=22)
        and m["max_gap_hours"] is not None
        and m["max_gap_hours"] <= OBS_MAX_GAP_HOURS
    )


def observation_samples_for_local_day(obs, day_start, day_end):
    samples = []
    for o in obs:
        try:
            t = _parse_iso_dt(o["ts"])
            t_local = t.astimezone(day_start.tzinfo)
            if day_start <= t_local < day_end:
                samples.append({"dt": t_local, "temperature_f": o["temperature_f"]})
        except Exception:
            continue
    return samples


def lock_forecast(city, info, today_local_str):
    log.info(f"lock {city} {today_local_str}")
    day_start, day_end = day_window_local(today_local_str, info["tz"])
    rec = {
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "station": info["station"],
        "local_date": today_local_str,
        "tz": info["tz"],
    }
    nws_periods = nws_hourly_periods(info)
    nws_samples = local_day_samples(nws_periods, day_start, day_end)
    if forecast_coverage_ok(nws_samples, day_start, day_end):
        nws_temps = [s["temperature_f"] for s in nws_samples]
        rec["fcst_nws_high"] = max(nws_temps)
        rec["fcst_nws_low"] = min(nws_temps)
    else:
        rec["fcst_nws_high"] = None
        rec["fcst_nws_low"] = None

    om = openmeteo_hourly(info)
    ens_high, ens_low = [], []
    if rec["fcst_nws_high"] is not None:
        ens_high.append(rec["fcst_nws_high"])
        ens_low.append(rec["fcst_nws_low"])
    for m in OPEN_METEO_MODELS:
        periods = om.get(m, [])
        samples = local_day_samples(periods, day_start, day_end) if periods else []
        if forecast_coverage_ok(samples, day_start, day_end):
            temps = [s["temperature_f"] for s in samples]
            hi, lo = max(temps), min(temps)
            rec[f"fcst_{m}_high"] = hi
            rec[f"fcst_{m}_low"] = lo
            ens_high.append(hi)
            ens_low.append(lo)
        else:
            rec[f"fcst_{m}_high"] = None
            rec[f"fcst_{m}_low"] = None
    rec["fcst_ensemble_high"] = sum(ens_high) / len(ens_high) if ens_high else None
    rec["fcst_ensemble_low"] = sum(ens_low) / len(ens_low) if ens_low else None
    rec["source_count"] = len(ens_high)
    log.info(
        f"  locked {city} {today_local_str} nws_high={rec['fcst_nws_high']} "
        f"ens_high={rec['fcst_ensemble_high']} ens_low={rec['fcst_ensemble_low']} "
        f"models_used={rec['source_count']}"
    )
    return rec


def reject_record(rec, reason, now_utc):
    row = dict(rec)
    row["rejected_at"] = now_utc.isoformat()
    row["reason"] = reason
    append_rejected(row)
    log.info(
        f"  rejected {rec['city']} {rec['local_date']} reason={reason} "
        f"n_obs={row.get('n_obs')} hours={row.get('obs_hours_covered')} "
        f"max_gap={row.get('obs_max_gap_hours')} source_count={row.get('source_count')}"
    )


def finalize(rec, now_utc=None):
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    log.info(f"finalize {rec['city']} {rec['local_date']}")
    day_start, day_end = day_window_local(rec["local_date"], rec["tz"])
    obs = nws_observations(
        rec["station"],
        day_start.astimezone(timezone.utc).isoformat(),
        day_end.astimezone(timezone.utc).isoformat(),
    )
    samples = observation_samples_for_local_day(obs, day_start, day_end)
    metrics = coverage_metrics(samples, day_start, day_end)
    rec["n_obs"] = metrics["n"]
    rec["obs_hours_covered"] = metrics["hours_covered"]
    rec["obs_max_gap_hours"] = (
        round(metrics["max_gap_hours"], 3) if metrics["max_gap_hours"] is not None else None
    )

    if not observation_coverage_ok(samples, day_start, day_end):
        reject_after = day_end.astimezone(timezone.utc) + timedelta(days=REJECT_AFTER_DAYS)
        if now_utc >= reject_after:
            reject_record(rec, "obs_incomplete", now_utc)
            return True
        log.warning(
            f"  obs incomplete for {rec['city']} {rec['local_date']}; will retry "
            f"n={rec['n_obs']} hours={rec['obs_hours_covered']} "
            f"max_gap={rec['obs_max_gap_hours']}"
        )
        return False

    if "source_count" not in rec:
        rec["source_count"] = sum(
            1 for s in ["nws"] + OPEN_METEO_MODELS
            if rec.get(f"fcst_{s}_high") is not None and rec.get(f"fcst_{s}_low") is not None
        )
    if not rec.get("source_count"):
        reject_record(rec, "no_valid_forecast", now_utc)
        return True

    temps = [s["temperature_f"] for s in samples]
    rec["actual_high"] = max(temps)
    rec["actual_low"] = min(temps)
    rec["finalized_at"] = now_utc.isoformat()
    for source in SOURCE_KEYS:
        fcst_high = rec.get(f"fcst_{source}_high")
        fcst_low = rec.get(f"fcst_{source}_low")
        if fcst_high is not None:
            rec[f"bias_{source}_high"] = rec["actual_high"] - fcst_high
        if fcst_low is not None:
            rec[f"bias_{source}_low"] = rec["actual_low"] - fcst_low
    append_paired(rec)
    log.info(
        f"  paired {rec['city']} {rec['local_date']} actual_high={rec['actual_high']:.1f} "
        f"actual_low={rec['actual_low']:.1f} bias_ens_high={rec.get('bias_ensemble_high')} "
        f"bias_ens_low={rec.get('bias_ensemble_low')}"
    )
    return True


def tick():
    locks = load_locks()
    now_utc = datetime.now(timezone.utc)
    for city, info in CITIES.items():
        tz = ZoneInfo(info["tz"])
        now_local = now_utc.astimezone(tz)
        today = now_local.strftime("%Y-%m-%d")
        key = f"{city}|{today}"
        # Lock once per day, only during the first 6h of local time.
        if key not in locks and 0 <= now_local.hour < 6:
            try:
                locks[key] = lock_forecast(city, info, today)
            except Exception as e:
                log.warning(f"lock {city} {today} failed: {e}")
        # Finalize past records once their local day ended at least 4h ago.
        for k in list(locks.keys()):
            try:
                c, d = k.split("|", 1)
            except ValueError:
                continue
            if c != city:
                continue
            _, d_end = day_window_local(d, info["tz"])
            if now_utc < d_end.astimezone(timezone.utc) + timedelta(hours=4):
                continue
            try:
                if finalize(locks[k], now_utc):
                    del locks[k]
            except Exception as e:
                log.warning(f"finalize {k} failed: {e}")
    save_locks(locks)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    log.info(f"audit start; data_dir={DATA_DIR} tick={TICK_SECONDS}s cities={list(CITIES)}")
    while True:
        try:
            tick()
        except Exception as e:
            log.exception(f"tick failed: {e}")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()

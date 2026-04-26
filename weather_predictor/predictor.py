"""
Paper-trade weather alpha against Polymarket bucket markets.

Architecture:
  - NWS hourly forecast cached per city, refreshed every 30 min
  - Polymarket weather market discovery every 5 min (gamma-api REST)
  - Real-time bid/ask via src/ws_feed.WSPriceFeed (push-based)
  - Decision re-evaluation every 30 s reading from feed
  - Settlement cycle every 1 h (NWS observations vs paper positions)

No real orders are placed. State written to:
  ./data/paper_decisions.csv   — every bucket evaluation per cycle
  ./data/paper_positions.json  — open paper positions
  ./data/paper_settled.csv     — closed positions with PnL

Run from repo root: python3 -m weather_predictor.predictor
"""

import csv
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from src.ws_feed import WSPriceFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("predictor")

# ── Paths ─────────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DECISIONS_CSV = DATA_DIR / "paper_decisions.csv"
POSITIONS_JSON = DATA_DIR / "paper_positions.json"
SETTLED_CSV = DATA_DIR / "paper_settled.csv"

# ── Tunables ──────────────────────────────────────────────────────────────────

CYCLE_SECONDS = 30                      # decision + orderbook poll
DISCOVERY_EVERY = 10                    # discovery every N cycles ≈ 5 min
FORECAST_REFRESH_EVERY = 60             # NWS refresh every N cycles ≈ 30 min
SETTLEMENT_EVERY = 120                  # settlement every N cycles ≈ 1 h

ENTRY_WINDOW_HOURS = (0.5, 12.0)        # broad outer bound; real gate is locked-σ + central-σ filter
# YES = long central bucket: tight edge bar, block high-price entries.
# NO  = short tail bucket: price near 1, ratio edge naturally small,
#       cap ask<0.95 so residual payoff covers spread+noise.
EDGE_THRESHOLD_YES = 1.5
EDGE_THRESHOLD_NO = 1.05
MAX_ENTRY_ASK_YES = 0.50
MAX_ENTRY_ASK_NO = 0.95
MIN_OUR_PROB_YES = 0.15
MIN_OUR_PROB_NO = 0.85
CENTRAL_SIGMA_MULT = 1.5                # YES only if bucket center ≤ this·σ from t_hat; NO only if outside
KELLY_DIVISOR = 4                       # quarter-Kelly
TIME_STOP_MIN_TO_CLOSE = 20             # in final N min, sell at bid if bid < entry*TIME_STOP_BID_FRAC
TIME_STOP_BID_FRAC = 0.5                # exit threshold vs entry inside final window
EXIT_EDGE_RATIO = 1.0                   # sell when our_prob / bid < this (market bid above fair value)
STOP_COOLDOWN_HOURS = 12                # after any exit, block re-entry of same token for this many hours
SIGMA_INFLATION = 1.5                   # multiplier on σ to account for forecast miss beyond published MAE

# In-memory cooldown: token_id → ts when the position was STOP'd. Rebuilt on start
# from paper_settled.csv so restarts don't wipe the block.
_recent_stops: dict[str, float] = {}

# Forecast std-dev (°F) as a function of lead time. Conservative estimates
# anchored on NOAA NDFD max-temperature verification (MAE) at typical forecast
# lead times, converted via σ ≈ MAE × 1.25 (Gaussian approximation).
# Reference: https://verification.nws.noaa.gov  (NDFD MaxT verification stats).
# These should be replaced with empirical σ per (city, lead_hours) once
# settlement data accumulates from paper-trade results.
_EMPIRICAL_SIGMA: float | None = None  # set at startup from settled RESOLVE residuals


def sigma_for_hours(h: float | None) -> float:
    if _EMPIRICAL_SIGMA is not None:
        return _EMPIRICAL_SIGMA
    if h is None:
        base = 3.0
    elif h <= 2:   base = 1.2
    elif h <= 4:   base = 1.5
    elif h <= 6:   base = 2.0
    elif h <= 12:  base = 2.5
    elif h <= 24:  base = 3.5
    else:          base = 4.5
    return base * SIGMA_INFLATION
MAX_PER_BUCKET_USD = 150.0              # single prediction cap
MAX_PER_MARKET_USD = 500.0              # all buckets in one market_date_city
PAPER_STARTING_CAPITAL = 10_000.0       # informational only
# Depth-of-book fill caps. Each fill level must satisfy BOTH:
#   level_price ≤ our_prob / MIN_FILL_EDGE   (per-share EV floor)
#   level_price ≤ top_ask × STUB_MULT        (anti-stub: don't walk far past a tiny top level)
# Sweep stops when next level fails either cap.
MIN_FILL_EDGE = 1.0
STUB_MULT = 3.0

# ── External APIs ─────────────────────────────────────────────────────────────

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
NWS = "https://api.weather.gov"
NWS_HEADERS = {
    "User-Agent": "polymarket-weather-paper-trader research (contact: local)",
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
HTTP_TIMEOUT = 15

# ── Cities ────────────────────────────────────────────────────────────────────
# Station codes match the *resolution source* used by Polymarket weather markets,
# verified empirically from gamma-api market.description field
# (e.g. "...at the LaGuardia Airport Station ... wunderground.com/.../KLGA").
# lat/lon are the airport station coordinates so NWS gridpoint forecast targets
# the same physical location the market resolves on.

CITIES: dict = {
    "Seattle":      {"lat": 47.4502, "lon": -122.3088, "station": "KSEA",
                     "keywords": ["seattle"]},
    "NYC":          {"lat": 40.7772, "lon": -73.8726,  "station": "KLGA",
                     "keywords": ["nyc", "new york"]},
    "Chicago":      {"lat": 41.9786, "lon": -87.9048,  "station": "KORD",
                     "keywords": ["chicago"]},
    "Dallas":       {"lat": 32.8471, "lon": -96.8517,  "station": "KDAL",
                     "keywords": ["dallas"]},
    "Atlanta":      {"lat": 33.6407, "lon": -84.4277,  "station": "KATL",
                     "keywords": ["atlanta"]},
    "Miami":        {"lat": 25.7959, "lon": -80.2870,  "station": "KMIA",
                     "keywords": ["miami"]},
    "LA":           {"lat": 33.9425, "lon": -118.4081, "station": "KLAX",
                     "keywords": ["los angeles", " la "]},
    "Houston":      {"lat": 29.6457, "lon": -95.2789,  "station": "KHOU",
                     "keywords": ["houston"]},
    "Denver":       {"lat": 39.7017, "lon": -104.7522, "station": "KBKF",
                     "keywords": ["denver"]},
    "Austin":       {"lat": 30.1945, "lon": -97.6699,  "station": "KAUS",
                     "keywords": ["austin"]},
    "SF":           {"lat": 37.6189, "lon": -122.3750, "station": "KSFO",
                     "keywords": ["san francisco"]},
    # Cities below have no current active Polymarket markets — kept for forward
    # compatibility; station codes are best-guess airport equivalents and must be
    # re-verified against market.description before relying on settlement.
    "Phoenix":      {"lat": 33.4373, "lon": -112.0078, "station": "KPHX",
                     "keywords": ["phoenix"]},
    "Boston":       {"lat": 42.3656, "lon": -71.0096,  "station": "KBOS",
                     "keywords": ["boston"]},
    "Philadelphia": {"lat": 39.8744, "lon": -75.2424,  "station": "KPHL",
                     "keywords": ["philadelphia", "philly"]},
    "DC":           {"lat": 38.8512, "lon": -77.0402,  "station": "KDCA",
                     "keywords": ["washington d.c.", "d.c.", " dc "]},
    "Minneapolis":  {"lat": 44.8848, "lon": -93.2223,  "station": "KMSP",
                     "keywords": ["minneapolis"]},
}


# ── NWS client ────────────────────────────────────────────────────────────────


def nws_get(url: str, params: dict | None = None) -> dict | None:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=NWS_HEADERS, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                logger.warning(f"NWS GET failed {url}: {e}")
                return None
            time.sleep(2)
    return None


def nws_resolve_grid(city: str, info: dict) -> dict | None:
    """Resolve lat/lon → grid + station via /points (one-shot per city)."""
    j = nws_get(f"{NWS}/points/{info['lat']},{info['lon']}")
    if not j:
        return None
    p = j.get("properties", {})
    return {
        "office": p.get("gridId"),
        "gx": p.get("gridX"),
        "gy": p.get("gridY"),
        "forecast_hourly_url": p.get("forecastHourly"),
        "tz": p.get("timeZone", "UTC"),
    }


def nws_fetch_hourly(grid: dict) -> list[dict]:
    """Returns [{startTime, temperature_f, ...}, ...] — empty list on failure."""
    url = grid.get("forecast_hourly_url")
    if not url:
        return []
    j = nws_get(url)
    if not j:
        return []
    periods = j.get("properties", {}).get("periods", []) or []
    out: list[dict] = []
    for p in periods:
        try:
            t = p.get("temperature")
            unit = p.get("temperatureUnit", "F")
            if t is None:
                continue
            t_f = float(t) if unit == "F" else float(t) * 9.0 / 5.0 + 32.0
            out.append({"startTime": p["startTime"], "temperature_f": t_f})
        except Exception:
            continue
    return out


def fetch_openmeteo_hourly(info: dict) -> dict[str, list[dict]]:
    """Returns {model_name: [{startTime, temperature_f}, ...]}. Empty on failure.
    Uses `timezone=auto` so startTime is a naive local datetime, matching how
    NWS forecast periods resolve via datetime.fromisoformat(...).date()."""
    try:
        r = requests.get(
            OPEN_METEO,
            params={
                "latitude": info["lat"],
                "longitude": info["lon"],
                "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "models": ",".join(OPEN_METEO_MODELS),
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        logger.debug(f"Open-Meteo fetch failed: {e}")
        return {}
    hourly = j.get("hourly", {}) or {}
    times = hourly.get("time") or []
    out: dict[str, list[dict]] = {}
    for model in OPEN_METEO_MODELS:
        temps = hourly.get(f"temperature_2m_{model}") or []
        if not temps or len(temps) != len(times):
            continue
        rows: list[dict] = []
        for t, temp in zip(times, temps):
            if temp is None:
                continue
            try:
                rows.append({"startTime": t, "temperature_f": float(temp)})
            except Exception:
                continue
        if rows:
            out[model] = rows
    return out


def nws_fetch_observations(station: str, start_iso: str, end_iso: str) -> list[dict]:
    """Returns [{ts, temperature_f}, ...] for the station between [start, end]."""
    j = nws_get(
        f"{NWS}/stations/{station}/observations",
        params={"start": start_iso, "end": end_iso},
    )
    if not j:
        return []
    out: list[dict] = []
    for f in j.get("features", []) or []:
        try:
            props = f.get("properties", {})
            t = props.get("temperature", {}).get("value")  # Celsius
            ts = props.get("timestamp")
            if t is None or not ts:
                continue
            out.append({"ts": ts, "temperature_f": float(t) * 9.0 / 5.0 + 32.0})
        except Exception:
            continue
    return out


def forecast_extreme_for_local_date(
    forecast: list[dict], local_date: str, kind: str
) -> float | None:
    """kind = 'highest' or 'lowest'. Filter periods whose local date matches.
    local_date is 'YYYY-MM-DD' interpreted in the period's own timezone offset."""
    matching: list[float] = []
    for p in forecast:
        try:
            dt = datetime.fromisoformat(p["startTime"])
            if dt.date().isoformat() == local_date:
                matching.append(p["temperature_f"])
        except Exception:
            continue
    if not matching:
        return None
    return max(matching) if kind == "highest" else min(matching)


# (station, local_date, kind) -> (fetch_ts, extreme_f). TTL keeps NWS load sane
# while the observed extreme still updates within the hour.
_OBS_CACHE: dict[tuple[str, str, str], tuple[float, float | None]] = {}
_OBS_TTL_SECONDS = 600


def observed_extreme_today(
    station: str, local_date: str, city_tz: ZoneInfo, kind: str
) -> float | None:
    """Observed max/min at the station between local midnight and now.
    None if the local day hasn't started or NWS has no data yet."""
    key = (station, local_date, kind)
    now_ts = time.time()
    cached = _OBS_CACHE.get(key)
    if cached and (now_ts - cached[0]) < _OBS_TTL_SECONDS:
        return cached[1]
    try:
        d = datetime.strptime(local_date, "%Y-%m-%d").date()
    except Exception:
        return None
    local_midnight = datetime.combine(d, datetime.min.time(), tzinfo=city_tz)
    now_utc = datetime.now(timezone.utc)
    if now_utc < local_midnight.astimezone(timezone.utc):
        _OBS_CACHE[key] = (now_ts, None)
        return None
    obs = nws_fetch_observations(
        station,
        local_midnight.astimezone(timezone.utc).isoformat(),
        now_utc.isoformat(),
    )
    temps: list[float] = []
    for o in obs:
        try:
            ts_dt = datetime.fromisoformat(o["ts"].replace("Z", "+00:00"))
            if ts_dt.astimezone(city_tz).date() == d:
                temps.append(o["temperature_f"])
        except Exception:
            continue
    val = None
    if temps:
        val = max(temps) if kind == "highest" else min(temps)
    _OBS_CACHE[key] = (now_ts, val)
    return val


# ── Polymarket discovery ──────────────────────────────────────────────────────


def _city_for_title(title_lc: str) -> str | None:
    for city, info in CITIES.items():
        for kw in info["keywords"]:
            if kw in title_lc:
                return city
    return None


def _kind_for_title(title_lc: str) -> str | None:
    if "highest temperature" in title_lc or "high temperature" in title_lc:
        return "highest"
    if "lowest temperature" in title_lc or "low temperature" in title_lc:
        return "lowest"
    return None


def fetch_weather_events() -> list[dict]:
    """Scan gamma-api /events for active weather temperature markets in our cities.
    Filters past-end events and dedups by event id."""
    seen_ids: set = set()
    out: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    offset = 0
    batch = 200
    for _ in range(20):
        try:
            r = requests.get(
                f"{GAMMA}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": batch,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "true",
                },
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            evs = r.json()
        except Exception as e:
            logger.warning(f"gamma /events fetch failed: {e}")
            break
        if not isinstance(evs, list) or not evs:
            break
        for ev in evs:
            ev_id = ev.get("id")
            if ev_id in seen_ids:
                continue
            end = ev.get("endDate") or ""
            if end and end < now_iso:
                continue
            title_lc = (ev.get("title") or "").lower()
            if "temperature" not in title_lc:
                continue
            city = _city_for_title(title_lc)
            if not city:
                continue
            kind = _kind_for_title(title_lc)
            if not kind:
                continue
            seen_ids.add(ev_id)
            out.append({"city": city, "kind": kind, "event": ev})
        if len(evs) < batch:
            break
        offset += batch
        time.sleep(0.2)
    return out


def get_orderbook(token_id: str, feed: WSPriceFeed) -> tuple[float | None, float | None]:
    """(bid, ask). ws_feed first, REST /book fallback."""
    bid = feed.get_bid(token_id)
    ask = feed.get_ask(token_id)
    if bid is not None and ask is not None:
        return bid, ask
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        bids = j.get("bids", []) or []
        asks = j.get("asks", []) or []
        rest_bid = max((float(b.get("price", 0)) for b in bids), default=None)
        rest_ask = min((float(a.get("price", 0)) for a in asks), default=None)
        return (bid if bid is not None else rest_bid,
                ask if ask is not None else rest_ask)
    except Exception as e:
        logger.debug(f"REST /book failed for {token_id[:12]}: {e}")
        return bid, ask


def fetch_asks(token_id: str) -> list[tuple[float, float]]:
    """Return ask ladder as [(price, size), ...] sorted ascending by price.
    Polymarket /book returns asks sorted descending (worst price first), so we
    reverse. Empty list on any failure."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        raw = r.json().get("asks", []) or []
        levels = []
        for a in raw:
            try:
                p = float(a.get("price", 0))
                s = float(a.get("size", 0))
            except (TypeError, ValueError):
                continue
            if p > 0 and s > 0:
                levels.append((p, s))
        levels.sort(key=lambda x: x[0])
        return levels
    except Exception as e:
        logger.debug(f"fetch_asks failed for {token_id[:12]}: {e}")
        return []


def sweep_asks(asks: list[tuple[float, float]], budget_usd: float) -> tuple[float, float, float]:
    """Walk the ask ladder cheapest-first up to budget_usd.
    Returns (total_shares, total_cost, avg_px). If the top level alone exceeds
    the budget, partially fills it. Caller decides whether avg_px still has edge."""
    if budget_usd <= 0 or not asks:
        return 0.0, 0.0, 0.0
    remaining = budget_usd
    shares = 0.0
    cost = 0.0
    for price, size in asks:
        level_cost = price * size
        if remaining >= level_cost:
            shares += size
            cost += level_cost
            remaining -= level_cost
        else:
            take = remaining / price
            shares += take
            cost += take * price
            remaining = 0.0
            break
    avg_px = (cost / shares) if shares > 0 else 0.0
    return shares, cost, avg_px


# ── Math + parsing ────────────────────────────────────────────────────────────


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def bucket_prob(t_hat: float, sigma: float, lo: float | None, hi: float | None) -> float:
    """Prob that realized temp lands in [lo, hi]. None lo = -inf, None hi = +inf."""
    cdf_hi = 1.0 if hi is None else normal_cdf(hi, t_hat, sigma)
    cdf_lo = 0.0 if lo is None else normal_cdf(lo, t_hat, sigma)
    return max(0.0, cdf_hi - cdf_lo)


def kelly_fraction(p: float, price: float) -> float:
    """Quarter-Kelly fraction of bankroll. 0 if no edge."""
    if price <= 0 or price >= 1 or p <= price:
        return 0.0
    full = (p - price) / (1.0 - price)
    return full / KELLY_DIVISOR


_BUCKET_RANGE_RE = re.compile(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*°?\s*F", re.I)
_BUCKET_HIGH_TAIL_RE = re.compile(
    r"(?:above|over|greater than|>=?|at least)\s*(\d{1,3})\s*°?\s*F", re.I
)
_BUCKET_HIGH_TAIL_RE2 = re.compile(r"(\d{1,3})\s*°?\s*F\s*(?:or above|or higher|\+)", re.I)
_BUCKET_LOW_TAIL_RE = re.compile(
    r"(?:below|under|less than|<=?|at most)\s*(\d{1,3})\s*°?\s*F", re.I
)
_BUCKET_LOW_TAIL_RE2 = re.compile(r"(\d{1,3})\s*°?\s*F\s*(?:or below|or lower)", re.I)


def parse_bucket(text: str) -> tuple[float | None, float | None] | None:
    """Returns (lo, hi). None on either end means open. None overall = unparseable."""
    if not text:
        return None
    m = _BUCKET_RANGE_RE.search(text)
    if m:
        return float(m.group(1)), float(m.group(2))
    for r in (_BUCKET_HIGH_TAIL_RE, _BUCKET_HIGH_TAIL_RE2):
        m = r.search(text)
        if m:
            return float(m.group(1)), None
    for r in (_BUCKET_LOW_TAIL_RE, _BUCKET_LOW_TAIL_RE2):
        m = r.search(text)
        if m:
            return None, float(m.group(1))
    return None


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_DATE_RE = re.compile(
    r"on\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})",
    re.I,
)


def parse_market_local_date(title: str, end_date_iso: str | None) -> str | None:
    """Returns 'YYYY-MM-DD' for the city's local resolution date.
    Prefers explicit 'on Month Day' from title; falls back to (endDate - 4h).date()."""
    if title:
        m = _DATE_RE.search(title)
        if m:
            month = _MONTHS[m.group(1).lower()]
            day = int(m.group(2))
            today = datetime.now(timezone.utc).date()
            year = today.year
            # if parsed month is far in the past, assume next year
            if month < today.month - 6:
                year += 1
            try:
                return f"{year:04d}-{month:02d}-{day:02d}"
            except Exception:
                pass
    if end_date_iso:
        try:
            dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            return (dt - timedelta(hours=4)).date().isoformat()
        except Exception:
            return None
    return None


# ── Paper book ────────────────────────────────────────────────────────────────


DECISION_FIELDS = [
    "ts", "city", "kind", "local_date", "market_slug", "bucket",
    "side", "token_id", "t_hat", "sigma", "our_prob",
    "bid", "ask", "edge_ratio", "hours_to_close",
    "action", "reason", "cost_usd", "shares",
]
SETTLED_FIELDS = [
    "ts_opened", "ts_settled", "city", "kind", "local_date",
    "market_slug", "bucket", "side", "token_id",
    "entry_price", "shares", "cost_usd",
    "exit_kind", "exit_price", "settle_temp_f",
    "won", "payout_usd", "pnl_usd",
    "t_hat_entry", "sigma_entry",
]


def load_positions() -> list[dict]:
    if not POSITIONS_JSON.exists():
        return []
    try:
        return json.loads(POSITIONS_JSON.read_text())
    except Exception:
        return []


def load_recent_stops() -> dict[str, float]:
    """Rebuild STOP cooldown map from paper_settled.csv so restarts don't wipe it."""
    if not SETTLED_CSV.exists():
        return {}
    cutoff = time.time() - STOP_COOLDOWN_HOURS * 3600
    stops: dict[str, float] = {}
    try:
        with SETTLED_CSV.open() as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("exit_kind") not in ("STOP", "TIME_STOP", "EDGE_EXIT"):
                    continue
                tid = r.get("token_id") or ""
                ts_str = r.get("ts_settled") or ""
                if not tid or not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str).timestamp()
                except Exception:
                    continue
                if ts >= cutoff:
                    stops[tid] = max(stops.get(tid, 0.0), ts)
    except Exception:
        return {}
    return stops


def save_positions(positions: list[dict]):
    POSITIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_JSON.write_text(json.dumps(positions, indent=2))


def append_csv(path: Path, fields: list[str], rows: list[dict]):
    if not rows:
        return
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


# ── Decision loop ─────────────────────────────────────────────────────────────


def _format_bucket(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is not None:
        return f"<={hi:.0f}"
    if hi is None and lo is not None:
        return f">={lo:.0f}"
    return f"{lo:.0f}-{hi:.0f}"


def _market_cost_so_far(positions: list[dict], city: str, kind: str, local_date: str) -> float:
    return sum(
        float(p.get("cost_usd", 0))
        for p in positions
        if p.get("status") == "OPEN"
        and p.get("city") == city
        and p.get("kind") == kind
        and p.get("local_date") == local_date
    )


def _hours_until(end_iso: str | None, now_ts: float) -> float | None:
    if not end_iso:
        return None
    try:
        dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return (dt.timestamp() - now_ts) / 3600.0
    except Exception:
        return None


def evaluate_city_kind(
    city: str,
    kind: str,
    event: dict,
    forecast: dict[str, list[dict]],
    observed_extreme: float | None,
    today_local_date: str,
    positions: list[dict],
    feed: WSPriceFeed,
    decisions_out: list[dict],
):
    """Evaluate every bucket in this event; may open/close positions in `positions`.
    `forecast` is {model_name: periods} — NWS + Open-Meteo ensemble members."""
    title = event.get("title", "")
    end_iso = event.get("endDate")
    local_date = parse_market_local_date(title, end_iso)
    if not local_date:
        return
    # Per-model daily extremes → ensemble mean (fcst_extreme) + spread (ensemble_std)
    model_extremes: list[float] = []
    for periods in forecast.values():
        e = forecast_extreme_for_local_date(periods, local_date, kind)
        if e is not None:
            model_extremes.append(e)
    if model_extremes:
        fcst_extreme: float | None = sum(model_extremes) / len(model_extremes)
        if len(model_extremes) > 1:
            m = fcst_extreme
            ensemble_std = (
                sum((x - m) ** 2 for x in model_extremes) / (len(model_extremes) - 1)
            ) ** 0.5
        else:
            ensemble_std = 0.0
    else:
        fcst_extreme = None
        ensemble_std = 0.0

    now_ts = time.time()
    hours = _hours_until(end_iso, now_ts)
    in_window = hours is not None and ENTRY_WINDOW_HOURS[0] <= hours <= ENTRY_WINDOW_HOURS[1]

    # Combine observed + ensemble-mean forecast when market resolves today.
    # Observed dominating remaining forecast → day's extreme effectively locked in.
    use_obs = (local_date == today_local_date and observed_extreme is not None)
    if use_obs and fcst_extreme is not None:
        if kind == "highest":
            t_hat = max(observed_extreme, fcst_extreme)
            locked = observed_extreme >= fcst_extreme
        else:
            t_hat = min(observed_extreme, fcst_extreme)
            locked = observed_extreme <= fcst_extreme
    elif use_obs:
        t_hat = observed_extreme
        locked = True
    else:
        t_hat = fcst_extreme
        locked = False

    # σ: locked overrides all (residual 1°F). Otherwise take the larger of the
    # lookup/empirical σ (historical prior) and the live ensemble spread — the
    # latter reflects current multi-model disagreement.
    sigma_base = sigma_for_hours(hours)
    if locked:
        sigma = 1.0
    else:
        sigma = max(sigma_base, ensemble_std)
    market_cost = _market_cost_so_far(positions, city, kind, local_date)
    market_slug = event.get("slug", "")

    # Parse all valid buckets in this event. our_prob is the raw Gaussian mass
    # in [lo,hi] — NO normalization across buckets. Polymarket omits tail
    # buckets when the forecast makes them unlikely; normalizing across only the
    # listed buckets inflates probabilities and creates fake edge.
    parsed: list[dict] = []
    for market in event.get("markets", []) or []:
        if not market.get("active", True) or market.get("closed"):
            continue
        q = market.get("question") or market.get("groupItemTitle") or ""
        bucket = parse_bucket(q)
        if not bucket:
            continue
        try:
            token_ids = json.loads(market.get("clobTokenIds") or "[]")
            outcomes = json.loads(market.get("outcomes") or "[]")
        except Exception:
            continue
        if len(token_ids) < 2 or len(outcomes) < 2:
            continue
        lo, hi = bucket
        raw = bucket_prob(t_hat, sigma, lo, hi) if t_hat is not None else None
        parsed.append({
            "market": market, "lo": lo, "hi": hi,
            "token_ids": token_ids, "outcomes": outcomes, "raw_prob": raw,
        })

    for item in parsed:
        market = item["market"]
        lo, hi = item["lo"], item["hi"]
        token_ids = item["token_ids"]
        outcomes = item["outcomes"]

        yes_idx = 0 if outcomes[0].lower() == "yes" else 1
        no_idx = 1 - yes_idx
        yes_tok = token_ids[yes_idx]
        no_tok = token_ids[no_idx]
        feed.subscribe([yes_tok, no_tok])

        our_prob_yes = item["raw_prob"]
        bucket_label = _format_bucket(lo, hi)

        for side, token_id, our_prob in (
            ("YES", yes_tok, our_prob_yes),
            ("NO", no_tok, (1.0 - our_prob_yes) if our_prob_yes is not None else None),
        ):
            bid, ask = get_orderbook(token_id, feed)
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "city": city, "kind": kind, "local_date": local_date,
                "market_slug": market_slug, "bucket": bucket_label,
                "side": side, "token_id": token_id,
                "t_hat": f"{t_hat:.2f}" if t_hat is not None else "",
                "sigma": f"{sigma:.2f}",
                "our_prob": f"{our_prob:.4f}" if our_prob is not None else "",
                "bid": f"{bid:.4f}" if bid is not None else "",
                "ask": f"{ask:.4f}" if ask is not None else "",
                "edge_ratio": "",
                "hours_to_close": f"{hours:.2f}" if hours is not None else "",
                "action": "EVAL", "reason": "",
                "cost_usd": "", "shares": "",
            }
            edge = (our_prob / ask) if (our_prob and ask and ask > 0) else None
            if edge is not None:
                row["edge_ratio"] = f"{edge:.3f}"

            existing = next(
                (p for p in positions if p.get("token_id") == token_id and p.get("status") == "OPEN"),
                None,
            )

            if existing:
                entry_px = float(existing.get("entry_price") or 0.0)
                minutes_left = hours * 60.0 if hours is not None else None
                # Time-stop: in the final TIME_STOP_MIN_TO_CLOSE minutes, if bid is
                # well below entry the model is unlikely to recover — take scraps.
                time_stop = (
                    minutes_left is not None and minutes_left <= TIME_STOP_MIN_TO_CLOSE
                    and bid is not None and bid > 0
                    and entry_px > 0 and bid < entry_px * TIME_STOP_BID_FRAC
                )
                if time_stop:
                    existing["status"] = "PENDING_EXIT"
                    existing["exit_price"] = bid
                    existing["exit_kind"] = "TIME_STOP"
                    existing["ts_exit"] = row["ts"]
                    _recent_stops[token_id] = now_ts
                    row["action"] = "EXIT"
                    row["reason"] = (
                        f"time_stop bid={bid:.4f}<entry*{TIME_STOP_BID_FRAC} "
                        f"{minutes_left:.0f}min_to_close"
                    )
                    decisions_out.append(row)
                    continue
                # Edge-reversal exit: if market bid is above our fair value
                # (our_prob / bid < EXIT_EDGE_RATIO), selling at bid ≥ EV of
                # holding. Uses live our_prob re-evaluated this cycle, not
                # entry snapshot — so stale edge / forecast drift both trigger.
                if (
                    bid is not None and bid > 0
                    and our_prob is not None
                    and (our_prob / bid) < EXIT_EDGE_RATIO
                ):
                    existing["status"] = "PENDING_EXIT"
                    existing["exit_price"] = bid
                    existing["exit_kind"] = "EDGE_EXIT"
                    existing["ts_exit"] = row["ts"]
                    _recent_stops[token_id] = now_ts
                    row["action"] = "EXIT"
                    row["reason"] = (
                        f"edge_exit our_p={our_prob:.3f}/bid={bid:.4f}="
                        f"{our_prob/bid:.2f}<{EXIT_EDGE_RATIO}"
                    )
                    decisions_out.append(row)
                    continue
                row["action"] = "HOLD"
                decisions_out.append(row)
                continue

            # Entry checks
            if our_prob is None or t_hat is None:
                row["reason"] = "no_forecast"
                decisions_out.append(row)
                continue
            if not in_window:
                row["reason"] = f"outside_entry_window({hours:.2f}h)" if hours is not None else "no_close_ts"
                decisions_out.append(row)
                continue
            if ask is None or ask <= 0:
                row["reason"] = "no_ask"
                decisions_out.append(row)
                continue
            max_ask = MAX_ENTRY_ASK_YES if side == "YES" else MAX_ENTRY_ASK_NO
            min_prob = MIN_OUR_PROB_YES if side == "YES" else MIN_OUR_PROB_NO
            edge_thresh = EDGE_THRESHOLD_YES if side == "YES" else EDGE_THRESHOLD_NO
            if ask > max_ask:
                row["reason"] = f"ask>{max_ask}"
                decisions_out.append(row)
                continue
            if our_prob < min_prob:
                row["reason"] = f"our_prob<{min_prob}"
                decisions_out.append(row)
                continue
            # Central/tail filter: forecast skill concentrates near t_hat; buy YES
            # only when bucket center is within ±CENTRAL_SIGMA_MULT·σ of t_hat, and
            # buy NO only when bucket is outside that band. Tail buckets (lo or hi
            # None) are treated as far from t_hat → YES rejected, NO allowed.
            center = None if (lo is None or hi is None) else (lo + hi) / 2.0
            if side == "YES":
                if center is None:
                    row["reason"] = "YES_on_tail_bucket"
                    decisions_out.append(row)
                    continue
                dist = abs(center - t_hat)
                if dist > CENTRAL_SIGMA_MULT * sigma:
                    row["reason"] = f"YES_bucket_{dist:.1f}F>{CENTRAL_SIGMA_MULT}*sigma"
                    decisions_out.append(row)
                    continue
            else:  # NO
                if center is not None:
                    dist = abs(center - t_hat)
                    if dist <= CENTRAL_SIGMA_MULT * sigma:
                        row["reason"] = f"NO_bucket_{dist:.1f}F<={CENTRAL_SIGMA_MULT}*sigma"
                        decisions_out.append(row)
                        continue
            last_stop = _recent_stops.get(token_id)
            if last_stop is not None and (now_ts - last_stop) < STOP_COOLDOWN_HOURS * 3600:
                age_h = (now_ts - last_stop) / 3600
                row["reason"] = f"stop_cooldown({age_h:.1f}h<{STOP_COOLDOWN_HOURS}h)"
                decisions_out.append(row)
                continue
            if edge is None or edge < edge_thresh:
                row["reason"] = f"edge<{edge_thresh}"
                decisions_out.append(row)
                continue
            if market_cost >= MAX_PER_MARKET_USD:
                row["reason"] = f"market_cap_hit(${market_cost:.0f})"
                decisions_out.append(row)
                continue

            f_kelly = kelly_fraction(our_prob, ask)
            kelly_usd = PAPER_STARTING_CAPITAL * f_kelly
            target_usd = min(kelly_usd, MAX_PER_BUCKET_USD, MAX_PER_MARKET_USD - market_cost)
            if target_usd < 1.0:
                row["reason"] = "kelly<$1"
                decisions_out.append(row)
                continue
            # Walk the real ask ladder — fills above top-of-book are expected
            # whenever Kelly budget exceeds top-level size. Re-check edge at the
            # resulting average price so a thin book with a deep second level
            # doesn't poison the trade.
            asks_ladder = fetch_asks(token_id)
            if not asks_ladder:
                row["reason"] = "no_ladder"
                decisions_out.append(row)
                continue
            top_ask_price = asks_ladder[0][0]
            max_fill_price = min(our_prob / MIN_FILL_EDGE, top_ask_price * STUB_MULT)
            allowed = [(p, s) for p, s in asks_ladder if p <= max_fill_price]
            if not allowed:
                row["reason"] = f"no_allowed_level(top={top_ask_price:.4f} cap={max_fill_price:.4f})"
                decisions_out.append(row)
                continue
            shares, cost_usd, avg_px = sweep_asks(allowed, target_usd)
            if shares <= 0 or cost_usd < 1.0 or avg_px <= 0:
                row["reason"] = f"sweep_empty(cap={max_fill_price:.4f})"
                decisions_out.append(row)
                continue
            if avg_px > max_ask:
                row["reason"] = f"avg_px={avg_px:.4f}>{max_ask}"
                decisions_out.append(row)
                continue
            avg_edge = our_prob / avg_px
            if avg_edge < edge_thresh:
                row["reason"] = f"avg_edge={avg_edge:.2f}<{edge_thresh}(top={edge:.2f})"
                decisions_out.append(row)
                continue
            new_pos = {
                "position_id": f"{token_id[:12]}-{int(now_ts)}",
                "ts_opened": row["ts"],
                "city": city, "kind": kind, "local_date": local_date,
                "market_slug": market_slug, "bucket": bucket_label,
                "side": side, "token_id": token_id,
                "entry_price": avg_px, "our_prob_at_entry": our_prob,
                "shares": shares, "cost_usd": cost_usd,
                "t_hat_entry": t_hat, "sigma_entry": sigma,
                "status": "OPEN",
            }
            positions.append(new_pos)
            market_cost += cost_usd
            row["action"] = "ENTER"
            row["reason"] = f"avg_edge={avg_edge:.2f}x top={edge:.2f}x kelly={f_kelly:.4f}"
            row["cost_usd"] = f"{cost_usd:.2f}"
            row["shares"] = f"{shares:.2f}"
            logger.info(
                f"ENTER {city} {kind} {local_date} {bucket_label} {side} "
                f"avg={avg_px:.4f} (top={ask:.4f})  our_prob={our_prob:.3f}  "
                f"avg_edge={avg_edge:.2f}x  ${cost_usd:.0f} ({shares:.1f}sh)"
            )
            decisions_out.append(row)


def decision_cycle(
    discovery: list[dict],
    forecast_cache: dict,
    grid_cache: dict[str, dict],
    positions: list[dict],
    feed: WSPriceFeed,
):
    decisions: list[dict] = []
    # One observation fetch per (city, kind) per cycle, TTL-cached inside helper
    obs_by_key: dict[tuple[str, str], float | None] = {}
    today_by_city: dict[str, str] = {}
    for entry in discovery:
        city, kind = entry["city"], entry["kind"]
        info = CITIES.get(city)
        grid = grid_cache.get(city)
        if not info or not grid:
            continue
        try:
            city_tz = ZoneInfo(grid.get("tz") or "UTC")
        except Exception:
            city_tz = timezone.utc
        today_str = datetime.now(city_tz).date().isoformat()
        today_by_city[city] = today_str
        key = (city, kind)
        if key not in obs_by_key:
            obs_by_key[key] = observed_extreme_today(
                info["station"], today_str, city_tz, kind
            )

    for entry in discovery:
        city, kind = entry["city"], entry["kind"]
        forecast = forecast_cache.get(city) or {}
        if not forecast:
            continue
        observed = obs_by_key.get((city, kind))
        today_str = today_by_city.get(city, "")
        try:
            evaluate_city_kind(
                city, kind, entry["event"], forecast,
                observed, today_str,
                positions, feed, decisions,
            )
        except Exception as e:
            logger.error(f"evaluate {city}/{kind} failed: {e}")
    append_csv(DECISIONS_CSV, DECISION_FIELDS, decisions)
    save_positions(positions)


# ── Calibration ───────────────────────────────────────────────────────────────


def compute_empirical_sigma() -> float | None:
    """Derive σ from historical |settle_temp - t_hat_entry| residuals.
    Prefers the t_hat_entry column; for older rows lacking it, backfills from
    paper_decisions.csv by matching token_id. Returns None if <10 pairs.

    Prefers the pinned calibration.json cache so σ survives CSV wipes."""
    cache_path = DATA_DIR / "calibration.json"
    if cache_path.exists():
        try:
            val = json.loads(cache_path.read_text()).get("empirical_sigma")
            if val is not None:
                return float(val)
        except Exception:
            pass
    if not SETTLED_CSV.exists():
        return None
    resolved: list[dict] = []
    try:
        with SETTLED_CSV.open() as f:
            for r in csv.DictReader(f):
                if r.get("exit_kind") == "RESOLVE" and r.get("settle_temp_f"):
                    resolved.append(r)
    except Exception:
        return None
    if len(resolved) < 10:
        return None

    residuals: list[float] = []
    need_backfill: dict[str, float] = {}  # token_id -> observed
    for r in resolved:
        try:
            obs = float(r["settle_temp_f"])
        except Exception:
            continue
        t_hat_str = r.get("t_hat_entry") or ""
        if t_hat_str:
            try:
                residuals.append(obs - float(t_hat_str))
                continue
            except Exception:
                pass
        tid = r.get("token_id") or ""
        if tid:
            need_backfill[tid] = obs

    if need_backfill and DECISIONS_CSV.exists():
        try:
            with DECISIONS_CSV.open() as f:
                for d in csv.DictReader(f):
                    if d.get("action") != "ENTER":
                        continue
                    tid = d.get("token_id") or ""
                    if tid not in need_backfill:
                        continue
                    try:
                        t_hat = float(d.get("t_hat") or "")
                    except Exception:
                        continue
                    residuals.append(need_backfill.pop(tid) - t_hat)
                    if not need_backfill:
                        break
        except Exception:
            pass

    if len(residuals) < 10:
        return None
    n = len(residuals)
    mean = sum(residuals) / n
    var = sum((x - mean) ** 2 for x in residuals) / (n - 1)
    return var ** 0.5


# ── Settlement ────────────────────────────────────────────────────────────────


def settle_positions(positions: list[dict], grid_cache: dict[str, dict]):
    """Settle PENDING_EXIT (early sell) and OPEN positions whose date has passed.
    grid_cache supplies city → {..., tz: 'America/New_York'} for local-date filtering."""
    if not positions:
        return
    today_utc = datetime.now(timezone.utc).date()
    settled_rows: list[dict] = []
    obs_cache: dict[tuple[str, str], float | None] = {}  # (station, local_date) -> extreme

    for pos in positions:
        if pos.get("status") not in ("OPEN", "PENDING_EXIT"):
            continue

        # Early-exit (PENDING_EXIT) — bid sale, no NWS needed
        if pos["status"] == "PENDING_EXIT":
            exit_price = float(pos.get("exit_price", 0))
            payout = pos["shares"] * exit_price
            pnl = payout - pos["cost_usd"]
            settled_rows.append({
                "ts_opened": pos["ts_opened"], "ts_settled": pos.get("ts_exit", ""),
                "city": pos["city"], "kind": pos["kind"], "local_date": pos["local_date"],
                "market_slug": pos["market_slug"], "bucket": pos["bucket"],
                "side": pos["side"], "token_id": pos["token_id"],
                "entry_price": f"{pos['entry_price']:.4f}",
                "shares": f"{pos['shares']:.4f}",
                "cost_usd": f"{pos['cost_usd']:.2f}",
                "exit_kind": pos.get("exit_kind", "STOP"),
                "exit_price": f"{exit_price:.4f}",
                "settle_temp_f": "",
                "won": "",
                "payout_usd": f"{payout:.2f}",
                "pnl_usd": f"{pnl:.2f}",
                "t_hat_entry": f"{pos.get('t_hat_entry', ''):.2f}" if pos.get("t_hat_entry") is not None else "",
                "sigma_entry": f"{pos.get('sigma_entry', ''):.2f}" if pos.get("sigma_entry") is not None else "",
            })
            pos["status"] = "SETTLED"
            continue

        # OPEN — needs NWS observation; only resolve once local day has fully passed
        try:
            local_date = datetime.strptime(pos["local_date"], "%Y-%m-%d").date()
        except Exception:
            continue

        info = CITIES.get(pos["city"])
        grid = grid_cache.get(pos["city"])
        if not info or not grid:
            continue
        try:
            city_tz = ZoneInfo(grid.get("tz") or "UTC")
        except Exception:
            city_tz = timezone.utc

        # Local day end → if not yet past, skip
        local_day_end = datetime.combine(
            local_date + timedelta(days=1), datetime.min.time(), tzinfo=city_tz
        )
        if datetime.now(timezone.utc) < local_day_end:
            continue

        station = info["station"]
        key = (station, pos["local_date"])
        if key not in obs_cache:
            # Query a 3-day UTC window centered on local day to ensure full coverage
            start_iso = (local_date - timedelta(days=1)).isoformat() + "T00:00:00+00:00"
            end_iso = (local_date + timedelta(days=2)).isoformat() + "T00:00:00+00:00"
            obs = nws_fetch_observations(station, start_iso, end_iso)
            day_temps: list[float] = []
            for o in obs:
                try:
                    ts_dt = datetime.fromisoformat(o["ts"].replace("Z", "+00:00"))
                    if ts_dt.astimezone(city_tz).date() == local_date:
                        day_temps.append(o["temperature_f"])
                except Exception:
                    continue
            if not day_temps:
                obs_cache[key] = None
            else:
                obs_cache[key] = max(day_temps) if pos["kind"] == "highest" else min(day_temps)
        extreme = obs_cache[key]
        if extreme is None:
            continue  # NWS data not yet available; retry next cycle

        # Determine win
        lo_str, _, hi_str = pos["bucket"].partition("-")
        if pos["bucket"].startswith("<="):
            lo, hi = None, float(pos["bucket"][2:])
        elif pos["bucket"].startswith(">="):
            lo, hi = float(pos["bucket"][2:]), None
        else:
            try:
                lo, hi = float(lo_str), float(hi_str)
            except Exception:
                continue
        # Polymarket convention: bucket "67-68" = realised in [67, 68) (inclusive low, exclusive high)
        in_bucket = (lo is None or extreme >= lo) and (hi is None or extreme < hi)
        yes_won = bool(in_bucket)
        won = yes_won if pos["side"] == "YES" else (not yes_won)
        payout = pos["shares"] if won else 0.0
        pnl = payout - pos["cost_usd"]
        settled_rows.append({
            "ts_opened": pos["ts_opened"],
            "ts_settled": datetime.now(timezone.utc).isoformat(),
            "city": pos["city"], "kind": pos["kind"], "local_date": pos["local_date"],
            "market_slug": pos["market_slug"], "bucket": pos["bucket"],
            "side": pos["side"], "token_id": pos["token_id"],
            "entry_price": f"{pos['entry_price']:.4f}",
            "shares": f"{pos['shares']:.4f}",
            "cost_usd": f"{pos['cost_usd']:.2f}",
            "exit_kind": "RESOLVE",
            "exit_price": "",
            "settle_temp_f": f"{extreme:.2f}",
            "won": "1" if won else "0",
            "payout_usd": f"{payout:.2f}",
            "pnl_usd": f"{pnl:.2f}",
            "t_hat_entry": f"{pos.get('t_hat_entry', ''):.2f}" if pos.get("t_hat_entry") is not None else "",
            "sigma_entry": f"{pos.get('sigma_entry', ''):.2f}" if pos.get("sigma_entry") is not None else "",
        })
        pos["status"] = "SETTLED"
        logger.info(
            f"SETTLE {pos['city']} {pos['kind']} {pos['local_date']} {pos['bucket']} "
            f"{pos['side']}  observed={extreme:.1f}°F  won={won}  pnl=${pnl:+.2f}"
        )

    append_csv(SETTLED_CSV, SETTLED_FIELDS, settled_rows)
    save_positions(positions)


# ── Main loop ─────────────────────────────────────────────────────────────────


def main():
    global _EMPIRICAL_SIGMA
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _EMPIRICAL_SIGMA = compute_empirical_sigma()
    if _EMPIRICAL_SIGMA is not None:
        logger.info(f"Empirical σ from settled RESOLVE = {_EMPIRICAL_SIGMA:.2f}°F (overrides lookup)")
    else:
        logger.info("Empirical σ unavailable (<10 resolved); using lookup by lead time")
    logger.info(
        f"Paper trader starting — {len(CITIES)} cities, cycle={CYCLE_SECONDS}s, "
        f"sigma=variable(by lead time), edge>={EDGE_THRESHOLD_YES}/{EDGE_THRESHOLD_NO}x (YES/NO), "
        f"max_ask={MAX_ENTRY_ASK_YES}/{MAX_ENTRY_ASK_NO} (YES/NO), "
        f"per-bucket cap=${MAX_PER_BUCKET_USD:.0f}"
    )

    # Resolve NWS gridpoint per city (one-shot)
    grid_cache: dict[str, dict] = {}
    for city, info in CITIES.items():
        g = nws_resolve_grid(city, info)
        if g:
            grid_cache[city] = g
            logger.info(f"  {city}: grid {g['office']}/{g['gx']},{g['gy']} tz={g['tz']}")
        else:
            logger.warning(f"  {city}: NWS gridpoint resolve failed; skipping")

    forecast_cache: dict[str, dict[str, list[dict]]] = {}
    discovery: list[dict] = []
    positions = load_positions()
    logger.info(f"Loaded {len(positions)} existing position(s)")
    _recent_stops.update(load_recent_stops())
    if _recent_stops:
        logger.info(f"Loaded {len(_recent_stops)} recent STOP(s) for cooldown")

    feed = WSPriceFeed()
    feed.start([])

    cycle_idx = 0
    while True:
        try:
            if cycle_idx % FORECAST_REFRESH_EVERY == 0:
                for city, g in grid_cache.items():
                    per_model: dict[str, list[dict]] = {}
                    nws = nws_fetch_hourly(g)
                    if nws:
                        per_model["nws"] = nws
                    om = fetch_openmeteo_hourly(CITIES[city])
                    per_model.update(om)
                    if per_model:
                        forecast_cache[city] = per_model
                model_count_sum = sum(len(v) for v in forecast_cache.values())
                logger.info(
                    f"Forecast refreshed: {len(forecast_cache)} cities, "
                    f"{model_count_sum} (city × model) feeds"
                )

            if cycle_idx % DISCOVERY_EVERY == 0:
                try:
                    discovery = fetch_weather_events()
                    logger.info(f"Discovery: {len(discovery)} city/kind events")
                except Exception as e:
                    logger.error(f"Discovery failed: {e}")

            decision_cycle(discovery, forecast_cache, grid_cache, positions, feed)

            if cycle_idx % SETTLEMENT_EVERY == 0 and cycle_idx > 0:
                settle_positions(positions, grid_cache)
        except Exception as e:
            logger.error(f"Cycle {cycle_idx} error: {e}")

        cycle_idx += 1
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()

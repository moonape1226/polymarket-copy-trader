"""
Weather-universe lightweight collector — Phase 2W.

Every 15 min, query gamma-api for active weather markets. Write one row per
market to data/book_logger/weather_universe/YYYY-MM-DD.jsonl. Promote a
bounded set of tokens into WatchSet using the spec's promotion rules.

Spec: docs/book_logger_spec.md §4.6 + §6 Phase 2W.
"""

import datetime
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from watch_set import WatchSet

logger = logging.getLogger(__name__)

_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
_DATA_DIR = os.environ.get("BOOK_LOGGER_DATA_DIR", "/data/book_logger")
_OUT_DIR = os.path.join(_DATA_DIR, "weather_universe")

_POLL_INTERVAL_S = int(os.environ.get("WEATHER_UNIVERSE_INTERVAL_S", "900"))
_SAMPLE_CAP = int(os.environ.get("WEATHER_FULL_BOOK_SAMPLE_CAP", "20"))
_MIN_LIQUIDITY = float(os.environ.get("WEATHER_FULL_BOOK_MIN_LIQUIDITY", "3000"))
_EXPIRY_HOURS = float(os.environ.get("WEATHER_FULL_BOOK_EXPIRY_HOURS", "24"))
_GRACE_AFTER_CLOSED_S = 3600

# Tight regex — catches the actual daily-bucket / weather-event question
# patterns Polymarket uses, while rejecting sports teams ("Carolina Hurricanes"),
# player names ("Jonas Wind"), book titles ("Winds of Winter"), etc.
_WEATHER_TITLE_RE = re.compile(
    r"(highest|lowest)\s+temperature\s+in\s+\w"
    r"|\btemperature\s+in\s+\w+\s+be\b"
    r"|\b(rainfall|snowfall|precipitation)\s+(in|on|by|exceed)"
    r"|\bhurricane\s+(make\s+landfall|landfall|form|hit)"
    r"|\b(major\s+)?hurricane\s+(by|before|in)\s+"
    r"|\bnamed\s+storm\s+forms?"
    r"|\btornado\s+(in|by|on|outbreak|hit|warning)"
    r"|\bwildfire\s+(in|by|burns)"
    r"|\bair\s+quality\s+(in|exceeds|reach)",
    re.IGNORECASE,
)
_WEATHER_TAG_LABELS = {
    "weather", "temperature", "weather-and-climate", "climate",
}

_write_lock = threading.Lock()
# market conditionId → first-closed-ts; cleared after _GRACE_AFTER_CLOSED_S
_grace_started: Dict[str, float] = {}
# market conditionId → last-seen-active-ts; used to detect markets that
# disappeared from the active set (review #1)
_seen_recently: Dict[str, float] = {}
# How long after a market was last seen active do we keep checking it
_DISAPPEAR_PROBE_WINDOW_S = 7200  # 2h — covers grace window + safety


def _is_weather(market: Dict[str, Any]) -> bool:
    title = market.get("question") or ""
    if _WEATHER_TITLE_RE.search(title):
        return True
    cat = (market.get("category") or "").lower()
    if cat == "weather":
        return True
    tags_raw = market.get("tags") or []
    if isinstance(tags_raw, str):
        try:
            tags_raw = json.loads(tags_raw)
        except Exception:
            return False
    for t in tags_raw or []:
        if isinstance(t, dict):
            label = (t.get("label") or t.get("slug") or "").lower()
            if label in _WEATHER_TAG_LABELS:
                return True
        elif isinstance(t, str):
            if t.lower() in _WEATHER_TAG_LABELS:
                return True
    return False


def _output_path(ts_ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
    return os.path.join(_OUT_DIR, dt.strftime("%Y-%m-%d.jsonl"))


def _write_row(row: Dict[str, Any]) -> None:
    path = _output_path(row["ts_ms"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with _write_lock:
        with open(path, "a") as f:
            f.write(line)


def _flt(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _parse_json_field(v: Any) -> list:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v if isinstance(v, list) else []


def _fetch_active_weather() -> List[Dict[str, Any]]:
    """Brute-force-paginate the entire active markets set. Polymarket has
    ~50k active markets at any time; default sort buries low-volume weather
    buckets way down, so we must scan the whole tail. ~100 requests per
    cycle at 500 per page; gamma-api comfortably handles this every 15 min.
    """
    out: List[Dict[str, Any]] = []
    offset = 0
    limit = 500
    pages_max = 200  # 100,000-market upper bound; safety guard against runaway
    total_scanned = 0
    for _ in range(pages_max):
        try:
            r = requests.get(
                _GAMMA_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=20,
            )
            if not r.ok:
                logger.warning(f"gamma fetch {r.status_code} at offset={offset}")
                break
            data = r.json()
            if not isinstance(data, list) or not data:
                break
            total_scanned += len(data)
            for m in data:
                if isinstance(m, dict) and _is_weather(m):
                    out.append(m)
            if len(data) < limit:
                break
            offset += limit
        except Exception as e:
            logger.warning(f"gamma fetch failed at offset={offset}: {e}")
            break
    logger.debug(f"weather_universe: scanned {total_scanned} active markets, matched {len(out)}")
    return out


def _build_row(market: Dict[str, Any], ts_ms: int) -> Dict[str, Any]:
    outcomes = _parse_json_field(market.get("outcomes"))
    token_ids = _parse_json_field(market.get("clobTokenIds"))
    prices = _parse_json_field(market.get("outcomePrices"))

    tokens_out = []
    for i, tid in enumerate(token_ids):
        tokens_out.append({
            "asset_id": str(tid),
            "outcome": outcomes[i] if i < len(outcomes) else None,
            "price_num": _flt(prices[i]) if i < len(prices) else None,
        })

    tags_raw = _parse_json_field(market.get("tags"))
    tag_names: list = []
    for t in tags_raw or []:
        if isinstance(t, dict):
            label = t.get("label") or t.get("slug")
            if label:
                tag_names.append(label)
        elif isinstance(t, str):
            tag_names.append(t)

    ts_iso = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")

    ev = market.get("event")
    event_id = market.get("eventId")
    if not event_id and isinstance(ev, dict):
        event_id = ev.get("id")

    return {
        "ts": ts_iso,
        "ts_ms": ts_ms,
        "market": market.get("conditionId"),
        "event_id": str(event_id) if event_id is not None else None,
        "slug": market.get("slug"),
        "title": market.get("question"),
        "category": market.get("category"),
        "tags": tag_names,
        "end_date_iso": market.get("endDate") or market.get("endDateIso"),
        "active": bool(market.get("active")),
        "closed": bool(market.get("closed")),
        "resolved": bool(market.get("umaResolutionStatus") == "resolved") if market.get("umaResolutionStatus") else None,
        "resolution_outcome": market.get("resolutionOutcome"),
        "tokens": tokens_out,
        "best_bid_num": _flt(market.get("bestBid")),
        "best_ask_num": _flt(market.get("bestAsk")),
        "volume_24h_num": _flt(market.get("volume24hr") or market.get("volume24hrClob")),
        "volume_total_num": _flt(market.get("volume") or market.get("volumeNum")),
        "liquidity_num": _flt(market.get("liquidity") or market.get("liquidityNum")),
        "source": "gamma_markets",
    }


def _within_expiry(market: Dict[str, Any], now_ms: int) -> bool:
    end_iso = market.get("endDate") or market.get("endDateIso") or ""
    if not end_iso:
        return False
    try:
        end_dt = datetime.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        end_ms = int(end_dt.timestamp() * 1000)
    except Exception:
        return False
    cutoff_ms = now_ms + int(_EXPIRY_HOURS * 3600 * 1000)
    return now_ms <= end_ms <= cutoff_ms


def _select_promotions(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Spec §4.6 promotion criteria — UNION of two paths (review #6):
      A) top-N by (liquidity desc, volume_24h desc, market asc), regardless of expiry
      B) within `_EXPIRY_HOURS` AND liquidity >= `_MIN_LIQUIDITY`
    Then dedup by conditionId, sort by the same key, cap at `_SAMPLE_CAP`.
    """
    now_ms = int(time.time() * 1000)
    candidates: dict = {}  # cid → (liq, vol_24, market)
    for m in markets:
        cid = m.get("conditionId") or ""
        if not cid:
            continue
        liq = _flt(m.get("liquidity") or m.get("liquidityNum")) or 0.0
        vol_24 = _flt(m.get("volume24hr") or m.get("volume24hrClob")) or 0.0
        in_expiry = _within_expiry(m, now_ms)
        # Path B: near-expiry + min liquidity
        b_qualifies = (liq >= _MIN_LIQUIDITY and in_expiry)
        # Path A: just keep — sort + cap will pick top-N regardless of expiry
        a_qualifies = True
        if a_qualifies or b_qualifies:
            existing = candidates.get(cid)
            if existing is None or liq > existing[0]:
                candidates[cid] = (liq, vol_24, m)
    ranked = sorted(candidates.values(), key=lambda c: (-c[0], -c[1], c[2].get("conditionId") or ""))
    return [c[2] for c in ranked[:_SAMPLE_CAP]]


def _promote_to_watchset(promotions: List[Dict[str, Any]], watch_set: WatchSet) -> int:
    n = 0
    for m in promotions:
        token_ids = _parse_json_field(m.get("clobTokenIds"))
        for tid in token_ids:
            watch_set.add(str(tid), source="weather_promote", state="active")
            n += 1
    return n


def _gc_grace_set(observed_market_ids: set) -> None:
    """Drop entries from _grace_started older than _GRACE_AFTER_CLOSED_S so the
    set doesn't grow unbounded across resolved markets."""
    now = time.time()
    drop = [m for m, ts in _grace_started.items()
            if (now - ts) > _GRACE_AFTER_CLOSED_S * 2]
    for m in drop:
        _grace_started.pop(m, None)


def _probe_disappeared(seen_now: set) -> List[Dict[str, Any]]:
    """Review #1: closed/resolved markets disappear from `active=true&closed=false`.
    For markets we saw active recently but missing this cycle, do a targeted
    lookup so we can write a final resolution row + start the 1h grace timer.

    Returns the freshly-fetched market dicts (caller writes rows + handles state).
    """
    now = time.time()
    cutoff = now - _DISAPPEAR_PROBE_WINDOW_S
    # cids we've seen in the last `_DISAPPEAR_PROBE_WINDOW_S` but didn't see this cycle
    candidates = [
        cid for cid, last_seen in _seen_recently.items()
        if last_seen >= cutoff and cid not in seen_now
    ]
    if not candidates:
        # GC stale registry entries
        drop = [c for c, ts in _seen_recently.items() if ts < cutoff]
        for c in drop:
            _seen_recently.pop(c, None)
        return []
    # Polymarket gamma supports comma-separated `condition_ids`. Batch in 50s.
    out: List[Dict[str, Any]] = []
    for i in range(0, len(candidates), 50):
        batch = candidates[i:i + 50]
        try:
            r = requests.get(
                _GAMMA_URL,
                params={"condition_ids": ",".join(batch)},
                timeout=15,
            )
            if not r.ok:
                continue
            data = r.json()
            if not isinstance(data, list):
                continue
            for m in data:
                if isinstance(m, dict):
                    out.append(m)
        except Exception as e:
            logger.warning(f"probe disappeared batch failed: {e}")
            continue
    return out


def run_loop(watch_set: WatchSet) -> None:
    logger.info(
        f"weather_universe started: poll={_POLL_INTERVAL_S}s "
        f"sample_cap={_SAMPLE_CAP} min_liq=${_MIN_LIQUIDITY:.0f} "
        f"expiry_hours={_EXPIRY_HOURS}"
    )
    while True:
        try:
            markets = _fetch_active_weather()
            now_s = time.time()
            now_ms = int(now_s * 1000)

            seen_ids: set = set()
            for m in markets:
                cid = m.get("conditionId")
                if cid:
                    seen_ids.add(cid)
                    _seen_recently[cid] = now_s

            # Review #1: probe markets that disappeared from the active set so
            # we capture closed/resolved transitions + final resolution rows.
            disappeared = _probe_disappeared(seen_ids)
            for m in disappeared:
                cid = m.get("conditionId")
                if not cid or not _is_weather(m):
                    continue
                is_closed = bool(m.get("closed")) or m.get("umaResolutionStatus") == "resolved"
                if is_closed:
                    if cid not in _grace_started:
                        _grace_started[cid] = now_s
                    elapsed = now_s - _grace_started[cid]
                    if elapsed <= _GRACE_AFTER_CLOSED_S:
                        # Within 1h grace: write final/resolution row
                        _write_row(_build_row(m, now_ms))

            _gc_grace_set(seen_ids)

            # Active markets — write a row each
            kept = [m for m in markets if m.get("conditionId")]
            for m in kept:
                _write_row(_build_row(m, now_ms))

            promotions = _select_promotions(kept)
            promoted_n = _promote_to_watchset(promotions, watch_set)

            logger.info(
                f"weather_universe: fetched={len(markets)} kept={len(kept)} "
                f"disappeared_probed={len(disappeared)} "
                f"promoted={len(promotions)} ({promoted_n} tokens) "
                f"watchset_size={watch_set.size()}"
            )
        except Exception as e:
            logger.exception(f"weather_universe loop error: {e}")
        time.sleep(_POLL_INTERVAL_S)


def start(watch_set: WatchSet) -> None:
    threading.Thread(target=run_loop, args=(watch_set,), daemon=True, name="weather_universe").start()

"""
Paper trader for market-scanner signals.

Reads /data/scan_history.json (maintained by market-scanner) and simulates
buy/PnL using:
  - entry price = VWAP from CLOB orderbook for $BET_USD walking the ask side
    (real book, not a constant-slip model)
  - cap: refuse fill if VWAP > scan_prob + MAX_SLIP_CENTS
  - Polymarket's 0.5% fee on profit at settlement

Writes:
  - /data/paper_positions.json   open + closed positions
  - /data/paper_trades.csv       one row per closed position

No on-chain action, no pmxt, no signing. Pure simulation of:
    "what would $X/bet have produced if applied to every scanner pick that
     passed our paper-trader filters and caps, filled at the book?"
"""

import csv
import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config (env-overridable) ─────────────────────────────────────────────────
TICK_SECONDS         = int(os.getenv("PAPER_TICK_SECONDS", "60"))
BET_USD              = float(os.getenv("PAPER_BET_USD", "200"))
MAX_POSITION_USD     = float(os.getenv("PAPER_MAX_POSITION_USD", "200"))
MAX_EVENT_USD        = float(os.getenv("PAPER_MAX_EVENT_USD", "600"))
MAX_TOPIC_USD        = float(os.getenv("PAPER_MAX_TOPIC_USD", "2000"))
MAX_TOTAL_USD        = float(os.getenv("PAPER_MAX_TOTAL_USD", "5000"))
MIN_REMAINING_HOURS  = float(os.getenv("PAPER_MIN_REMAINING_HOURS", "24"))
MAX_SLIP_CENTS       = float(os.getenv("PAPER_MAX_SLIP_CENTS", "1"))
FEE_PCT              = float(os.getenv("PAPER_FEE_PCT", "0.005"))
GAMMA_BASE           = os.getenv("GAMMA_BASE", "https://gamma-api.polymarket.com")
CLOB_BASE            = os.getenv("CLOB_BASE", "https://clob.polymarket.com")
BOOK_TIMEOUT_SEC     = float(os.getenv("PAPER_BOOK_TIMEOUT_SEC", "8"))
# 95-100% band re-enabled 2026-06-08: near-certain winners are the only bucket
# that survives the win/loss asymmetry (avg win +$40 vs avg loss -$200).
PROB_BAND_BLOCKLIST  = {s.strip() for s in os.getenv("PAPER_PROB_BAND_BLOCKLIST", "").split(",") if s.strip()}
# musk added 2026-06-08: scan_prob systematically over-states tweet-count
# buckets (paper 3/6 vs scanner 89%); -$494 over 11 days.
TOPIC_BLOCKLIST      = {s.strip() for s in os.getenv("PAPER_TOPIC_BLOCKLIST", "crypto,musk").split(",") if s.strip()}
SLACK_WEBHOOK        = os.getenv("SLACK_WEBHOOK_URL")
SCAN_HISTORY_FILE    = Path(os.getenv("SCAN_HISTORY_FILE", "/data/scan_history.json"))
# Distinct from weather_predictor's /data/paper_positions.json — namespaced to
# the scanner-driven paper trader.
POSITIONS_FILE       = Path(os.getenv("POSITIONS_FILE", "/data/scanner_paper_positions.json"))
TRADES_CSV           = Path(os.getenv("TRADES_CSV", "/data/scanner_paper_trades.csv"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)


# ── Topic classifier (kept inline to keep container independent) ─────────────
_META_TOPICS = [
    ("iran",   re.compile(r"iran|hormuz|bab el.mandeb|kharg", re.I)),
    ("musk",   re.compile(r"elon musk|musk\s+(tweets|post|#)", re.I)),
    ("crypto", re.compile(r"bitcoin|\bbtc\b|ethereum|\beth\b|solana|\bsol\b|"
                          r"xrp|\bcrypto\b|dogecoin|cardano|bnb|avalanche|litecoin", re.I)),
    ("ai",     re.compile(r"deepseek|openai|anthropic|ai model|chatgpt|\bclaude\b|gemini", re.I)),
]


def classify_topic(text: str) -> str:
    for k, p in _META_TOPICS:
        if p.search(text or ""):
            return k
    return "other"


def prob_band(p: float) -> str:
    b = int(p * 100) // 5 * 5
    return f"{b}-{b+5}%"


# ── IO helpers ───────────────────────────────────────────────────────────────
def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise


def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            logger.warning("paper_positions.json unreadable, starting empty")
    return {"open": [], "closed": []}


def load_history() -> list:
    if not SCAN_HISTORY_FILE.exists():
        return []
    try:
        return json.loads(SCAN_HISTORY_FILE.read_text())
    except Exception:
        logger.warning("scan_history.json unreadable this tick")
        return []


# In-memory mirror of position_ids already written to TRADES_CSV. Populated
# lazily on first append from disk so a process restart that re-attempts an
# unconfirmed close does not produce a duplicate CSV row.
_LOGGED_POSITION_IDS: set | None = None
_TRADE_CSV_COLS = ["position_id", "closed_at", "event", "question", "topic", "side_label",
                   "scan_prob", "entry_price", "size_usd", "shares",
                   "outcome", "net_pnl", "duration_hours"]


def _logged_position_ids() -> set:
    global _LOGGED_POSITION_IDS
    if _LOGGED_POSITION_IDS is not None:
        return _LOGGED_POSITION_IDS
    seen: set = set()
    if TRADES_CSV.exists():
        try:
            with TRADES_CSV.open("r", newline="") as f:
                for row in csv.DictReader(f):
                    pid = row.get("position_id")
                    if pid:
                        seen.add(pid)
        except Exception as e:
            logger.warning(f"could not read existing trades csv: {e}")
    _LOGGED_POSITION_IDS = seen
    return seen


def append_trade_row(rec: dict) -> None:
    pid = rec.get("position_id", "")
    seen = _logged_position_ids()
    if pid and pid in seen:
        return  # already logged; close-retry under crash is idempotent
    TRADES_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_TRADE_CSV_COLS)
        if new:
            w.writeheader()
        w.writerow({k: rec.get(k, "") for k in _TRADE_CSV_COLS})
    if pid:
        seen.add(pid)


# ── CLOB book / token lookup ─────────────────────────────────────────────────
# (market_id, side_label_lower) -> token_id. Cached because the mapping never
# changes for a given market and gamma /markets/{id} is the slow path.
_TOKEN_CACHE: dict = {}


def fetch_token_id(market_id: str, side_label: str):
    """Returns token_id for the given side, or None.

    Negative cache only persists *permanent* (schema-shape) failures so the
    market is silenced for the process lifetime — transient errors (timeout,
    429, 5xx, network) are returned as None for this call but NOT cached,
    so the next tick retries.
    """
    if not market_id or not side_label:
        return None
    key = (market_id, side_label.lower())
    if key in _TOKEN_CACHE:
        return _TOKEN_CACHE[key]
    try:
        r = requests.get(f"{GAMMA_BASE}/markets/{market_id}", timeout=BOOK_TIMEOUT_SEC)
    except requests.RequestException as e:
        logger.warning(f"fetch_token_id transient (network) for {market_id}: {e}")
        return None  # do NOT cache — retry next tick

    if r.status_code in (429,) or r.status_code >= 500:
        logger.warning(f"fetch_token_id transient HTTP {r.status_code} for {market_id}")
        return None  # do NOT cache — retry next tick
    if not r.ok:
        # 4xx other than 429: market truly unavailable, cache to suppress.
        logger.warning(f"fetch_token_id HTTP {r.status_code} for {market_id}; caching no-token")
        _TOKEN_CACHE[key] = None
        return None

    try:
        m = r.json()
        outcomes = json.loads(m.get("outcomes", "[]"))
        token_ids = json.loads(m.get("clobTokenIds", "[]"))
    except (ValueError, TypeError) as e:
        # malformed payload: schema problem, cache.
        logger.warning(f"fetch_token_id parse failure for {market_id}: {e}; caching no-token")
        _TOKEN_CACHE[key] = None
        return None

    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        _TOKEN_CACHE[key] = None  # schema mismatch — permanent
        return None
    for idx, o in enumerate(outcomes):
        if isinstance(o, str) and o.lower() == side_label.lower():
            _TOKEN_CACHE[key] = token_ids[idx]
            return token_ids[idx]
    # side label not found in this market: permanent mismatch.
    _TOKEN_CACHE[key] = None
    return None


def fetch_book_vwap(token_id: str, size_usd: float):
    """Walk the ask side of the CLOB book; return (vwap_price, top_ask_price,
    top_level_depth_usd) for a buy of size_usd. None if book empty or shallow."""
    try:
        r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id},
                         timeout=BOOK_TIMEOUT_SEC)
        r.raise_for_status()
        book = r.json()
    except Exception as e:
        logger.warning(f"fetch_book_vwap failed for {token_id[:10]}…: {e}")
        return None

    asks_raw = book.get("asks", []) or []
    levels = []
    for a in asks_raw:
        try:
            levels.append((float(a["price"]), float(a["size"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not levels:
        return None
    # Polymarket CLOB sometimes returns asks descending; sort ascending = best first.
    levels.sort(key=lambda x: x[0])

    top_price = levels[0][0]
    top_depth_usd = levels[0][0] * levels[0][1]

    remaining = size_usd
    cost = 0.0
    shares = 0.0
    for price, size in levels:
        capacity_usd = price * size
        spend = min(remaining, capacity_usd)
        cost += spend
        shares += spend / price
        remaining -= spend
        if remaining <= 1e-6:
            break
    if remaining > 1e-6 or shares <= 0:
        return None  # book too shallow to fill the bet
    vwap = cost / shares
    return (vwap, top_price, top_depth_usd)


# ── Decision logic ───────────────────────────────────────────────────────────
def filtered_reason(rec: dict, now_ts: float):
    sp = rec.get("scan_prob")
    if not isinstance(sp, (int, float)):
        return "malformed-record"
    band = prob_band(sp)
    if band in PROB_BAND_BLOCKLIST:
        return f"band-block({band})"
    topic = classify_topic(rec.get("event", ""))
    if topic in TOPIC_BLOCKLIST:
        return f"topic-block({topic})"
    remaining_h = (rec.get("expiry_ts", 0) - now_ts) / 3600
    if remaining_h < MIN_REMAINING_HOURS:
        return f"expiry-too-soon"
    return None


def cap_blocked(rec: dict, size: float, open_positions: list):
    topic = classify_topic(rec.get("event", ""))
    ev = rec.get("event", "")
    same_event = sum(p.get("size_usd", 0) for p in open_positions if p.get("event") == ev)
    same_topic = sum(p.get("size_usd", 0) for p in open_positions if p.get("topic") == topic)
    total_open = sum(p.get("size_usd", 0) for p in open_positions)
    if size > MAX_POSITION_USD:
        return "position-cap"
    if same_event + size > MAX_EVENT_USD:
        return f"event-cap"
    if same_topic + size > MAX_TOPIC_USD:
        return f"topic-cap({topic})"
    if total_open + size > MAX_TOTAL_USD:
        return "total-cap"
    return None


# ── Open / close ─────────────────────────────────────────────────────────────
def open_position(rec: dict, state: dict, now_iso: str) -> str:
    """Returns 'opened' on success or a short skip-reason string on failure
    (no-token / no-book / missed-price)."""
    side = rec.get("scan_side", "")
    token_id = fetch_token_id(rec.get("market_id", ""), side)
    if not token_id:
        return "no-token"
    book = fetch_book_vwap(token_id, BET_USD)
    if not book:
        return "no-book"
    vwap, top_ask, top_depth_usd = book

    max_acceptable = min(0.99, rec["scan_prob"] + MAX_SLIP_CENTS / 100)
    if vwap > max_acceptable:
        logger.info(f"SKIP  vwap={vwap:.3f} > max={max_acceptable:.3f} "
                    f"(top {top_ask:.3f}, depth ${top_depth_usd:.0f})  "
                    f"{rec.get('question','')[:50]}")
        return "missed-price"

    shares = BET_USD / vwap
    pos = {
        "position_id":     str(uuid.uuid4()),
        "condition_id":    rec["condition_id"],
        "market_id":       rec.get("market_id", ""),
        "token_id":        token_id,
        "event":           rec.get("event", ""),
        "question":        rec.get("question", ""),
        "topic":           classify_topic(rec.get("event", "")),
        "side_label":      side,
        "scan_prob":       rec["scan_prob"],
        "entry_price":     vwap,
        "book_top_ask":    top_ask,
        "book_top_depth":  top_depth_usd,
        "size_usd":        BET_USD,
        "shares":          shares,
        "entered_at":      now_iso,
        "expiry_ts":       rec.get("expiry_ts", 0),
    }
    state["open"].append(pos)
    slip_bps = (vwap - rec["scan_prob"]) * 10000
    logger.info(f"BUY  ${BET_USD:>5.0f}  @{vwap:.4f} "
                f"(scan {rec['scan_prob']:.3f}, slip {slip_bps:+.0f}bp, "
                f"top {top_ask:.3f}/${top_depth_usd:.0f})  "
                f"[{pos['topic']:<6}] [{side:>3}]  {pos['question'][:45]}")
    return "opened"


def close_position(pos: dict, hist_rec: dict, state: dict, now_iso: str) -> None:
    won = bool(hist_rec.get("correct"))
    entry = pos["entry_price"]
    shares = pos["shares"]
    if won:
        gross_profit = shares * (1.0 - entry)
        fee = gross_profit * FEE_PCT
        net = gross_profit - fee
        outcome = "win"
    else:
        net = -pos["size_usd"]
        outcome = "loss"

    dur_h = 0.0
    try:
        e_ts = datetime.fromisoformat(pos["entered_at"].replace("Z", "+00:00")).timestamp()
        c_ts = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).timestamp()
        dur_h = (c_ts - e_ts) / 3600
    except Exception:
        pass

    closed = {**pos,
              "outcome":        outcome,
              "net_pnl":        net,
              "winner":         hist_rec.get("winner"),
              "closed_at":      now_iso,
              "duration_hours": dur_h}
    # Persist CSV first so a crash before state mutation merely re-attempts the
    # close next tick (idempotent via condition_id), rather than leaving a
    # JSON-closed-but-CSV-missing position.
    append_trade_row(closed)
    state["closed"].append(closed)
    state["open"] = [p for p in state["open"] if p["position_id"] != pos["position_id"]]
    sign = "+" if net >= 0 else "-"
    logger.info(f"CLOSE {sign}${abs(net):>5.2f}  [{pos['topic']:<6}] "
                f"{outcome:<4}  {pos['question'][:55]}")


# ── Tick ─────────────────────────────────────────────────────────────────────
def tick() -> None:
    history = load_history()
    if not history:
        return
    state = load_positions()
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    now_iso = now.isoformat()
    hist_by_cid = {r.get("condition_id"): r for r in history if r.get("condition_id")}

    # 1) settle any open positions whose history record is now resolved
    opened = closed = 0
    for pos in list(state["open"]):
        hr = hist_by_cid.get(pos["condition_id"])
        if hr and hr.get("resolved") and hr.get("correct") is not None:
            close_position(pos, hr, state, now_iso)
            closed += 1

    # 2) consider opening new positions
    seen_cids = {p["condition_id"] for p in state["open"]} | \
                {p["condition_id"] for p in state["closed"]}
    skipped: dict = {}

    for rec in history:
        if rec.get("resolved"):
            continue
        cid = rec.get("condition_id")
        if not cid or cid in seen_cids:
            continue
        reason = filtered_reason(rec, now_ts)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        cap = cap_blocked(rec, BET_USD, state["open"])
        if cap:
            skipped[cap] = skipped.get(cap, 0) + 1
            continue
        result = open_position(rec, state, now_iso)
        if result == "opened":
            seen_cids.add(cid)
            opened += 1
        else:
            skipped[result] = skipped.get(result, 0) + 1
            # Do NOT add to seen_cids — retry on next tick (book may refill)

    # 3) persist
    atomic_write_json(POSITIONS_FILE, state)

    # 4) heartbeat — also surface positions in surprising states.
    open_sum = sum(p.get("size_usd", 0) for p in state["open"])
    cum_pnl = sum(p.get("net_pnl", 0) for p in state["closed"])
    wins = sum(1 for p in state["closed"] if p.get("outcome") == "win")
    n_closed = len(state["closed"])
    msg = (f"tick: open {len(state['open'])} (${open_sum:.0f})  "
           f"closed {n_closed} ({wins}/{n_closed})  cum ${cum_pnl:+.2f}")
    if opened or closed:
        msg += f"  | this tick +{opened}/-{closed}"
    logger.info(msg)
    if skipped:
        top = sorted(skipped.items(), key=lambda kv: -kv[1])[:4]
        logger.info("  skipped: " + ", ".join(f"{r}={n}" for r, n in top))

    # Stuck-state surfacing:
    #   settle-pending: history says resolved=True but correct=None (rare; partial)
    #   book-stuck:     expiry_ts is in the past but history still has resolved=False
    settle_pending = []
    book_stuck = []
    for pos in state["open"]:
        hr = hist_by_cid.get(pos.get("condition_id"))
        if hr and hr.get("resolved") and hr.get("correct") is None:
            settle_pending.append(pos)
        elif pos.get("expiry_ts", 0) < now_ts and (not hr or not hr.get("resolved")):
            book_stuck.append(pos)
    if settle_pending or book_stuck:
        bits = []
        if settle_pending: bits.append(f"settle-pending={len(settle_pending)}")
        if book_stuck:     bits.append(f"book-stuck={len(book_stuck)}")
        logger.warning("  stuck: " + ", ".join(bits))

    # 5) optional daily Slack
    _maybe_post_daily(state, now)


# ── Slack daily summary (once per UTC day) ──────────────────────────────────
_last_daily_date = None


def _maybe_post_daily(state: dict, now) -> None:
    global _last_daily_date
    if not SLACK_WEBHOOK:
        return
    today = now.strftime("%Y-%m-%d")
    if _last_daily_date == today:
        return
    closed = state["closed"]
    if not closed:
        _last_daily_date = today
        return
    wins = [p for p in closed if p["outcome"] == "win"]
    losses = [p for p in closed if p["outcome"] == "loss"]
    cumulative = sum(p["net_pnl"] for p in closed)
    open_count = len(state["open"])
    open_usd = sum(p["size_usd"] for p in state["open"])
    text = (f"*paper-trader daily — {today}*\n"
            f"```\n"
            f"open positions: {open_count} (${open_usd:.0f})\n"
            f"closed total:   {len(closed)} ({len(wins)} win / {len(losses)} loss)\n"
            f"cumulative PnL: ${cumulative:+,.2f}\n"
            f"```")
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10).raise_for_status()
    except Exception as e:
        logger.warning(f"Slack post failed: {e}")
    _last_daily_date = today


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info(
        f"paper-trader starting — bet ${BET_USD:.0f}, "
        f"caps pos/${MAX_POSITION_USD:.0f} ev/${MAX_EVENT_USD:.0f} "
        f"topic/${MAX_TOPIC_USD:.0f} total/${MAX_TOTAL_USD:.0f}, "
        f"max slip {MAX_SLIP_CENTS}¢ (book VWAP), fee {FEE_PCT*100:.1f}%, "
        f"band-block {sorted(PROB_BAND_BLOCKLIST) or 'none'}, "
        f"topic-block {sorted(TOPIC_BLOCKLIST) or 'none'}, tick {TICK_SECONDS}s"
    )
    while True:
        try:
            tick()
        except Exception:
            logger.exception("tick failed")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()

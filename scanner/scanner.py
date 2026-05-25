"""
Market opportunity scanner.

Every hour:
  1. Scans Polymarket for high-probability near-term markets.
     - New markets are reported immediately.
     - Existing markets are re-reported only if probability shifted >= 5%.
     - Per event: tracks top 10 markets by liquidity (reduces bucket-market noise).
  2. Checks previously tracked markets that have since resolved, records whether
     the high-probability side was correct, and logs calibration stats.

Criteria:
  - Probability: 80-97%
  - Days to expiry: <= 7
  - Volume: > $5K, Liquidity: > $3K
  - Excludes sports & esports (tag + title heuristic) and a small novelty
    title blocklist. Crypto-price markets (e.g. "Bitcoin above $X") are
    intentionally INCLUDED — they are the bulk of the high-prob near-term
    signal and the calibration history depends on them.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")

GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_BASE = "https://polymarket.com/event"
SCAN_INTERVAL_MIN = 15

MIN_PROB = 0.80
MAX_PROB = 0.97
MAX_DAYS = 7
MIN_VOLUME = 5_000
MIN_LIQ = 3_000
MAX_MARKETS_PER_EVENT = 3
NOTIFY_PROB_DELTA = 0.05

HISTORY_FILE = Path("/data/scan_history.json")

SKIP_TITLE_KEYWORDS = [
    "gta vi", "before gta", "jesus christ", "will jesus",
]

# Fallback when the event's sports/esports tag is missing on gamma API.
# Historical backtest: single-match sports markets are the largest loss category
# (Magic vs. Celtics, Man U vs. Leeds, LoL BO3, CS IEM, etc.).
SPORTS_TITLE_PATTERNS = [
    re.compile(r"\bvs\.?\b", re.IGNORECASE),
    re.compile(r"\(BO[35]\)", re.IGNORECASE),
    re.compile(r"^(LoL|CS|Counter-Strike|Dota|NBA|NFL|MLB|NHL|UFC|MMA)\s*[:\-]", re.IGNORECASE),
]


def looks_like_sports_matchup(title: str) -> bool:
    return any(p.search(title) for p in SPORTS_TITLE_PATTERNS)


# Crypto coin-price strike markets ("Bitcoin above ___ on May 7?",
# "What price will Solana hit in April?", "Will Ethereum reach $2,600 in April?")
# are a confirmed net-negative slice: 46/52 (88.5%), EV -1.7%, vs ex-crypto
# EV +9.3% over the 4/22-5/19 dataset. A title is treated as crypto-price only
# when it carries BOTH a coin token AND a price/direction hint, so crypto-adjacent
# non-price markets (e.g. "Solana ETF approved by June?") are intentionally kept.
_CRYPTO_COIN = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|"
    r"cardano|ada|bnb|avalanche|avax|litecoin|ltc)\b", re.IGNORECASE
)
_CRYPTO_PRICE_HINT = re.compile(
    r"(above|below|\bdip\b|reach|\bhit\b|\$\s?\d|price will|be above|be below|\bprice\b)",
    re.IGNORECASE,
)


def looks_like_crypto_price(text: str) -> bool:
    t = text or ""
    return bool(_CRYPTO_COIN.search(t) and _CRYPTO_PRICE_HINT.search(t))


# Observational only: group resolved events by topic to expose portfolio-level
# concentration. A stretch of Iran/Musk events resolving together is one
# correlated bet, not N independent ones — the topic breakdown makes that visible.
META_TOPICS = [
    ("iran",      re.compile(r"iran|hormuz|bab el.mandeb|kharg", re.I)),
    ("musk",      re.compile(r"elon musk|musk\s+(tweets|post|#)", re.I)),
    ("ai",        re.compile(r"deepseek|openai|anthropic|ai model|chatgpt|\bclaude\b|gemini", re.I)),
    ("crypto",    re.compile(r"bitcoin|\bbtc\b|ethereum|\beth\b|solana|\bcrypto\b", re.I)),
    ("elections", re.compile(r"\belection|electoral", re.I)),
    ("trump",     re.compile(r"trump", re.I)),
]


def classify_topic(text: str) -> str:
    for topic, pat in META_TOPICS:
        if pat.search(text or ""):
            return topic
    return "other"


# ── History file ─────────────────────────────────────────────────────────────

RESOLVED_RETENTION_DAYS = 30


_HISTORY_BAK = HISTORY_FILE.with_suffix(".json.bak")


def _load_json_list(path: Path) -> list | None:
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"load_history: {path} unreadable/corrupt: {e}")
        return None
    if not isinstance(data, list):
        logger.warning(f"load_history: {path} is not a list ({type(data).__name__})")
        return None
    return data


def load_history() -> list:
    if HISTORY_FILE.exists():
        data = _load_json_list(HISTORY_FILE)
        if data is not None:
            return data
        logger.error("load_history: main corrupt — trying .bak")
    # main missing (crash between rotate and replace) or corrupt: recover
    # from the last known-good backup before save_history overwrites with []
    if _HISTORY_BAK.exists():
        data = _load_json_list(_HISTORY_BAK)
        if data is not None:
            logger.warning("load_history: recovered calibration from .bak")
            return data
    if HISTORY_FILE.exists() or _HISTORY_BAK.exists():
        logger.error("load_history: no recoverable history; starting empty")
    return []


def save_history(records: list):
    cutoff = datetime.now(timezone.utc).timestamp() - RESOLVED_RETENTION_DAYS * 86400
    records = [
        r for r in records
        if not r.get("resolved") or r.get("expiry_ts", 0) > cutoff
    ]
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 1. write+fsync the new content to a temp file FIRST
    tmp = HISTORY_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(records, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    # 2. refresh .bak only from a *valid* current file — never overwrite a
    #    good backup with a corrupt/truncated main (A7)
    if HISTORY_FILE.exists() and _load_json_list(HISTORY_FILE) is not None:
        try:
            os.replace(HISTORY_FILE, _HISTORY_BAK)
        except OSError as e:
            logger.warning(f"save_history: could not refresh .bak: {e}")
    # 3. atomically swap the new content into place
    os.replace(tmp, HISTORY_FILE)


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_events() -> list:
    all_events = []
    offset = 0
    batch_size = 100  # gamma-api hard-caps /events at 100/page regardless of limit
    pages = 40  # 40×100 keeps the prior ~4000-event depth
    hit_cap = True
    for _ in range(pages):
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{GAMMA_API}/events",
                    params={"active": "true", "limit": batch_size, "offset": offset,
                            "order": "volume", "ascending": "false"},
                    timeout=30,
                )
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                logger.warning(f"fetch_events attempt {attempt + 1} failed: {e} — retrying in 10s")
                time.sleep(10)
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            hit_cap = False
            break
        all_events.extend(batch)
        if len(batch) < batch_size:
            hit_cap = False
            break
        offset += batch_size
        time.sleep(0.3)
    if hit_cap:
        # Exhausted the page budget before reaching the end of the active set —
        # lower-volume but still-eligible near-term markets may be unseen (#120)
        logger.warning(
            f"fetch_events: hit {pages}-page cap ({len(all_events)} events); "
            f"tail of active set not scanned"
        )
    return all_events


def fetch_market(condition_id: str, market_id: str = "") -> dict | None:
    # /markets?conditionId is broken in gamma API (returns wrong market).
    # Use /markets/{id} when available; fall back to conditionId search only as last resort.
    try:
        if market_id:
            resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=15)
            resp.raise_for_status()
            m = resp.json()
            # Validate this path too — a stale/wrong stored market_id would
            # otherwise resolve the history row against the wrong market (S1)
            if isinstance(m, dict) and m.get("conditionId") == condition_id:
                return m
            logger.warning(
                f"fetch_market: /markets/{market_id} conditionId mismatch "
                f"(want {condition_id[:10]}, got {m.get('conditionId', '?')[:10] if isinstance(m, dict) else '?'}) — skipping"
            )
            return None
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # This endpoint can return the WRONG market — only accept a result
        # whose conditionId actually matches what we asked for, else the
        # history row gets resolved against an unrelated market (B-HIGH).
        candidates = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for m in candidates:
            if isinstance(m, dict) and m.get("conditionId") == condition_id:
                return m
        if candidates:
            logger.warning(
                f"fetch_market: conditionId mismatch for {condition_id[:10]} "
                f"(got {candidates[0].get('conditionId', '?')[:10] if isinstance(candidates[0], dict) else '?'}) — skipping"
            )
        return None
    except Exception as e:
        # Distinguish API/network/schema failure from "market not found" so a
        # stalled calibration is visible instead of silent (P2).
        logger.warning(
            f"fetch_market failed (cid={condition_id[:10]} mid={market_id}): {e}"
        )
    return None


# ── Resolution check ─────────────────────────────────────────────────────────

def check_resolutions(history: list) -> tuple[list, int]:
    """Update unresolved records that have now settled. Returns (history, newly_resolved_count)."""
    pending = [r for r in history if not r.get("resolved")]
    if not pending:
        return history, 0

    now = datetime.now(timezone.utc).timestamp()
    updated = 0
    for rec in pending:
        expiry = rec.get("expiry_ts", 0)
        if now < expiry:
            continue
        market = fetch_market(rec["condition_id"], rec.get("market_id", ""))
        if not market:
            continue
        try:
            prices = [float(p) for p in json.loads(market.get("outcomePrices", "[]"))]
            outcomes = json.loads(market.get("outcomes", "[]"))
        except Exception:
            continue
        if not prices:
            continue
        # Prefer explicit resolution flags; fall back to the price heuristic so
        # a resolved market with a stale/non-0.99 price is not missed (#185).
        # NOT `closed`: it only means trading stopped/expired, not resolved —
        # a closed-but-pending market (e.g. 0.62/0.38) must not be scored (A3).
        resolved_flag = (
            market.get("umaResolutionStatus") == "resolved"
            or market.get("resolved") is True
        )
        if not resolved_flag and max(prices) < 0.99:
            continue  # not yet resolved to binary
        winner_idx = prices.index(max(prices))
        winner = outcomes[winner_idx] if winner_idx < len(outcomes) else "?"
        rec["resolved"] = True
        rec["winner"] = winner
        rec["correct"] = winner == rec["scan_side"]
        rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
        updated += 1
        status = "CORRECT" if rec["correct"] else "WRONG"
        logger.info(
            f"  [{status}]  {rec['scan_prob']*100:.0f}% {rec['scan_side']} → "
            f"won: {winner}  —  {rec['question'][:65]}"
        )

    if updated:
        logger.info(f"Resolved {updated} market(s) since last scan.")
    return history, updated


def calibration_stats(history: list, send_slack: bool = False):
    resolved = [r for r in history if r.get("resolved")]
    if not resolved:
        return
    correct = sum(1 for r in resolved if r.get("correct"))
    total = len(resolved)
    logger.info(f"── Calibration: {correct}/{total} correct ({correct/total*100:.1f}%)")

    buckets: dict = {}
    for r in resolved:
        p = r["scan_prob"]
        band = f"{int(p*100)//5*5}-{int(p*100)//5*5+5}%"
        buckets.setdefault(band, {"n": 0, "ok": 0})
        buckets[band]["n"] += 1
        if r.get("correct"):
            buckets[band]["ok"] += 1
    for band in sorted(buckets):
        b = buckets[band]
        logger.info(f"    {band:8}  {b['ok']}/{b['n']} ({b['ok']/b['n']*100:.0f}%)")

    if SLACK_WEBHOOK and send_slack:
        _send_calibration_slack(resolved, correct, total, buckets)


def event_correct_stats(history: list):
    """Collapse bucket events into one verdict per event to undo the correlation
    between [No] picks on the same question (e.g. 12 Masters candidates).
    An event is 'all-correct' only if every resolved market in it resolved correctly.
    Also prints topic-level concentration (Iran/Musk/AI/…) so the skew from
    highly-correlated clusters is visible alongside the raw count.
    """
    if not history:
        return

    all_by_event: dict = {}
    for r in history:
        # group by stable event_slug; fall back to title for old records (M2)
        all_by_event.setdefault(r.get("event_slug") or r.get("event", "?"), []).append(r)

    topic_stats: dict = {}
    for ev, rs in all_by_event.items():
        topic = classify_topic(ev)
        ts = topic_stats.setdefault(topic, {"events": 0, "markets": 0, "resolved": 0, "correct": 0})
        ts["events"] += 1
        ts["markets"] += len(rs)
        for r in rs:
            if r.get("resolved"):
                ts["resolved"] += 1
                if r.get("correct"):
                    ts["correct"] += 1
    logger.info("── By topic:")
    for topic, ts in sorted(topic_stats.items(), key=lambda kv: -kv[1]["events"]):
        wr = f"{ts['correct']}/{ts['resolved']}" if ts["resolved"] else "—"
        logger.info(
            f"    {topic:10}  {ts['events']:2d} events / {ts['markets']:3d} markets  [resolved {wr}]"
        )

    # Only score events where EVERY tracked market has resolved. Previously
    # unresolved siblings were dropped, so a 2-of-5-resolved event could be
    # reported as "fully correct" prematurely (#269).
    complete = {ev: rs for ev, rs in all_by_event.items()
                if rs and all(r.get("resolved") for r in rs)}
    partial_n = sum(1 for ev, rs in all_by_event.items()
                    if any(r.get("resolved") for r in rs)
                    and not all(r.get("resolved") for r in rs))
    if not complete:
        if partial_n:
            logger.info(f"── Event-level: 0 complete events ({partial_n} partial, not scored)")
        return

    events = [(ev, rs, all(r.get("correct") for r in rs)) for ev, rs in complete.items()]
    ok = sum(1 for _, _, c in events if c)
    total = len(events)
    logger.info(
        f"── Event-level: {ok}/{total} complete events fully correct "
        f"({ok/total*100:.1f}%); {partial_n} partial event(s) not yet scored"
    )

    single = [(ev, rs, c) for ev, rs, c in events if len(rs) == 1]
    bucket = [(ev, rs, c) for ev, rs, c in events if len(rs) > 1]
    if single:
        s_ok = sum(1 for _, _, c in single if c)
        logger.info(f"    single-market events: {s_ok}/{len(single)} ({s_ok/len(single)*100:.0f}%)")
    if bucket:
        b_ok = sum(1 for _, _, c in bucket if c)
        logger.info(f"    bucket events:        {b_ok}/{len(bucket)} ({b_ok/len(bucket)*100:.0f}%)")
        for ev, rs, c in sorted(bucket, key=lambda x: -len(x[1])):
            c_ok = sum(1 for r in rs if r.get("correct"))
            mark = "✓" if c else "✗"
            logger.info(f"      {mark} {c_ok}/{len(rs)}  {ev[:65]}")


# ── Scan ──────────────────────────────────────────────────────────────────────

def scan(history: list) -> list:
    now = datetime.now(timezone.utc).timestamp()
    try:
        events = fetch_events()
    except Exception as e:
        logger.error(f"Failed to fetch events: {e}")
        return history

    existing_cids = {r["condition_id"] for r in history}
    cid_to_rec = {r["condition_id"]: r for r in history}
    # Track how many markets each event already has in history so the
    # MAX_MARKETS_PER_EVENT cap constrains total, not just per-scan additions.
    # key by event_slug (fall back to title for old records) so the cap is
    # not mis-applied when titles collide or change (S2)
    existing_per_event: dict = {}
    for r in history:
        ev = r.get("event_slug") or r.get("event", "")
        existing_per_event[ev] = existing_per_event.get(ev, 0) + 1

    # First pass: collect all qualifying markets per event
    event_candidates: dict = {}
    parse_skipped = 0

    for event in events:
        title = event.get("title", "")
        if any(k in title.lower() for k in SKIP_TITLE_KEYWORDS):
            continue
        # tags/markets keys can be present but null — `or []` guards against a
        # crash outside the per-market try (M3)
        tag_slugs = {t.get("slug", "") for t in (event.get("tags") or [])}
        if tag_slugs & {"sports", "esports"}:
            continue
        if looks_like_sports_matchup(title):
            continue
        if looks_like_crypto_price(title):
            continue
        event_slug = event.get("slug", "")
        url = f"{POLYMARKET_BASE}/{event_slug}"

        for market in (event.get("markets") or []):
            try:
                prices = [float(p) for p in json.loads(market.get("outcomePrices", "[]"))]
                if not prices:
                    continue
                max_p = max(prices)
                if not (MIN_PROB <= max_p <= MAX_PROB):
                    continue
                vol = float(market.get("volume", 0))
                if vol < MIN_VOLUME:
                    continue
                liq = float(market.get("liquidity", 0))
                if liq < MIN_LIQ:
                    continue
                end_str = market.get("endDate", "").replace("Z", "").split(".")[0]
                end_ts = datetime.strptime(end_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                days = (end_ts - now) / 86400
                if not (0 < days <= MAX_DAYS):
                    continue
                max_idx = prices.index(max_p)
                outcomes = json.loads(market.get("outcomes", "[]"))
                side = outcomes[max_idx] if max_idx < len(outcomes) else "?"
                cid = market.get("conditionId", "")
                if not cid:
                    # no conditionId → can't track/resolve it uniquely; empty
                    # string would collapse many markets onto one key (M4)
                    parse_skipped += 1
                    continue
                # Per-market guard: a generic event title can hide a coin-price
                # strike that only shows in the question (e.g. "$80,000").
                if looks_like_crypto_price(market.get("question", "")):
                    continue

                entry = {
                    "question": market.get("question", ""),
                    "days": days,
                    "prob": max_p,
                    "ret": (1 / max_p - 1) * 100,
                    "vol": vol,
                    "liq": liq,
                    "side": side,
                    "condition_id": cid,
                    "market_id": market.get("id", ""),
                    "end_ts": end_ts,
                }

                if event_slug not in event_candidates:
                    event_candidates[event_slug] = {
                        "title": title, "url": url, "days": days, "markets": []
                    }
                event_candidates[event_slug]["markets"].append(entry)
                event_candidates[event_slug]["days"] = min(event_candidates[event_slug]["days"], days)
            except Exception as e:
                # schema variation / bad date / non-JSON field — count so a
                # systematic gamma change is visible, not silently dropped (#330)
                parse_skipped += 1
                if parse_skipped <= 3:
                    logger.warning(f"scan: skipped market parse: {type(e).__name__}: {e}")
                continue
    if parse_skipped:
        logger.warning(f"scan: {parse_skipped} market(s) skipped on parse this cycle")

    # Second pass: per event keep top MAX_MARKETS_PER_EVENT by liquidity,
    # add new ones to history, determine notification eligibility
    grouped: dict = {}

    for slug, ev_data in event_candidates.items():
        top_markets = sorted(ev_data["markets"], key=lambda m: m["liq"], reverse=True)[:MAX_MARKETS_PER_EVENT]
        has_notable = False

        ev_title = ev_data["title"]
        for entry in top_markets:
            cid = entry["condition_id"]
            max_p = entry["prob"]
            is_new = cid not in existing_cids

            if is_new:
                if existing_per_event.get(slug, 0) >= MAX_MARKETS_PER_EVENT:
                    continue
                rec: dict = {
                    "condition_id": cid,
                    "market_id": entry["market_id"],
                    "question": entry["question"],
                    "event": ev_title,
                    "event_slug": slug,  # stable group key; title can collide (M2)
                    "scan_prob": max_p,
                    "scan_side": entry["side"],
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "expiry_ts": entry["end_ts"],
                    "resolved": False,
                    "winner": None,
                    "correct": None,
                    "last_notified_prob": None,
                }
                history.append(rec)
                existing_cids.add(cid)
                cid_to_rec[cid] = rec
                existing_per_event[slug] = existing_per_event.get(slug, 0) + 1

            rec = cid_to_rec.get(cid, {})
            # Fall back to scan_prob so existing records without last_notified_prob
            # don't flood notifications on first run with new code.
            last_notified = rec.get("last_notified_prob") or rec.get("scan_prob")
            entry["is_new"] = is_new
            entry["prev_prob"] = last_notified
            entry["should_notify"] = (
                is_new
                or last_notified is None
                or abs(max_p - last_notified) >= NOTIFY_PROB_DELTA
            )
            if entry["should_notify"]:
                has_notable = True

        grouped[slug] = {**ev_data, "markets": top_markets, "has_notable": has_notable}

    # Only report events with new or significantly changed markets
    events_list = sorted(
        [e for e in grouped.values() if e["has_notable"]],
        key=lambda e: e["days"],
    )

    if not events_list:
        logger.info("No new or updated opportunities.")
        return history

    logger.info(f"{'='*80}")
    logger.info(f"  SCAN RESULTS — {len(events_list)} event(s) with updates")
    logger.info(f"{'='*80}")
    for ev in events_list:
        logger.info(f"  [{ev['days']:.1f}d]  {ev['title']}  ({ev['url']})")
        for m in sorted(ev["markets"], key=lambda x: -x["ret"]):
            if not m.get("should_notify"):
                continue
            if m["is_new"]:
                tag = " [NEW]"
            else:
                delta = (m["prob"] - m["prev_prob"]) * 100
                tag = f" [{delta:+.0f}%]"
            logger.info(
                f"    {m['prob']*100:.0f}%  +{m['ret']:.1f}%  "
                f"${m['liq']/1000:.0f}K liq  [{m['side']}]  {m['question']}{tag}"
            )
    logger.info(f"{'='*80}")

    # New/updated markets are log-only by request — no Slack push. The
    # calibration summary still goes to Slack via _send_calibration_slack.
    # Logging counts as notified so last_notified_prob bookkeeping (and the
    # ≥5% re-log dedup) keeps working.
    notified = True

    if notified:
        for ev in events_list:
            for m in ev["markets"]:
                if m.get("should_notify"):
                    rec = cid_to_rec.get(m["condition_id"])
                    if rec is not None:
                        rec["last_notified_prob"] = m["prob"]

    return history


# ── Slack ─────────────────────────────────────────────────────────────────────

def _send_slack(events_list: list) -> bool:
    """Returns True only if the alert was actually delivered (A4)."""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Market Scanner — {len(events_list)} event(s) updated"},
        }
    ]
    for ev in events_list:
        lines = []
        for m in sorted(ev["markets"], key=lambda x: -x["ret"]):
            if not m.get("should_notify"):
                continue
            if m["is_new"]:
                tag = " [NEW]"
            else:
                delta = (m["prob"] - m["prev_prob"]) * 100
                tag = f" [{delta:+.0f}%]"
            lines.append(
                f"  • {m['prob']*100:.0f}% [{m['side']}]  +{m['ret']:.1f}%  "
                f"${m['liq']/1000:.0f}K liq  —  {m['question']}{tag}"
            )
        if not lines:
            continue
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{ev['url']}|{ev['title']}>*  _{ev['days']:.1f}d_\n```" + "\n".join(lines) + "```",
            },
        })
    # Slack rejects payloads >50 blocks — split into chunks so a large scan
    # doesn't fail the whole alert (M1). Keep the header only on the first.
    header, body = blocks[0], blocks[1:]
    chunks: list[list] = []
    for i in range(0, max(1, len(body)), 48):
        part = body[i:i + 48]
        chunks.append(([header] + part) if i == 0 else part)
    ok = True
    for idx, chunk in enumerate(chunks):
        try:
            requests.post(SLACK_WEBHOOK, json={"blocks": chunk}, timeout=10).raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send Slack notification (part {idx+1}/{len(chunks)}): {e}")
            ok = False
    if ok:
        logger.info(f"Slack notification sent ({len(chunks)} message(s)).")
    return ok


def _send_calibration_slack(resolved: list, correct: int, total: int, buckets: dict):
    recently = sorted(
        [r for r in resolved if r.get("resolved_at")],
        key=lambda r: r.get("resolved_at", ""),
        reverse=True,
    )[:5]

    bucket_lines = "\n".join(
        f"  {band:8}  {b['ok']}/{b['n']} ({b['ok']/b['n']*100:.0f}%)"
        for band, b in sorted(buckets.items())
    )
    recent_lines = "\n".join(
        f"  {'✓' if r['correct'] else '✗'}  {r['scan_prob']*100:.0f}% [{r['scan_side']}] → {r['winner']}  {r['question'][:55]}"
        for r in recently
    )
    failed = sorted(
        [r for r in resolved if r.get("resolved_at") and not r.get("correct")],
        key=lambda r: r.get("resolved_at", ""),
        reverse=True,
    )
    failed_section = ""
    if failed:
        failed_lines = "\n".join(
            f"  ✗  {r['scan_prob']*100:.0f}% [{r['scan_side']}] → {r['winner']}  {r['question'][:55]}"
            for r in failed
        )
        failed_section = f"\n\nFailed resolutions ({len(failed)}):\n{failed_lines}"
    text = (
        f"*Calibration update — {correct}/{total} correct ({correct/total*100:.1f}%)*\n"
        f"```\nBy probability band:\n{bucket_lines}\n\nRecent resolutions:\n{recent_lines}{failed_section}\n```"
    )
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10).raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send calibration Slack: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info(
        f"Scanner started — criteria: {MIN_PROB*100:.0f}–{MAX_PROB*100:.0f}% prob, "
        f"≤{MAX_DAYS}d expiry, >${MIN_VOLUME/1000:.0f}K vol. Excludes: sports, esports. Interval: {SCAN_INTERVAL_MIN}min."
    )
    while True:
        history = load_history()
        logger.info(f"Tracking {len(history)} market(s) ({sum(1 for r in history if r.get('resolved'))} resolved).")
        history, newly_resolved = check_resolutions(history)
        calibration_stats(history, send_slack=newly_resolved > 0)
        event_correct_stats(history)
        history = scan(history)
        save_history(history)
        logger.info(f"Next scan in {SCAN_INTERVAL_MIN}min.")
        time.sleep(SCAN_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()

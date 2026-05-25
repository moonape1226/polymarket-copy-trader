"""Backtest the A+B change (drop in-sample bias/sigma, clamp our_prob to
[0.05, 0.95]) against historical paper data.

Two passes:
  (a) settled — scan every row in paper_settled.csv (~207 entries), recover
      raw ensemble t_hat by subtracting whichever bias policy was active at
      ts_opened, recompute σ proxy, apply A+B gate. For entries the old bot
      made that A+B would skip, report avoided PnL (using actual settled
      pnl). One-sided (only sees would-skip, not would-add).

  (b) eval — scan available EVAL rows (9.5 days: archive_20260421 +
      paper_decisions.csv.1 + .csv). For each, compute old vs new decision.
      Bi-directional but limited window. For "would-enter" rows that match an
      IEM-resolvable market, compute hold-to-RESOLVE PnL using actual entry
      price as the ask.

Approximation: ensemble_std wasn't logged. The recorded `sigma` is
max(in-sample σ floor, ensemble_std). We use `max(recorded_sigma, 1.0)` as
new σ — over-estimates ensemble_std when the in-sample floor was binding,
making A+B's bucket probabilities flatter than they'd actually be. Conservative
in the "block fewer" direction; documented in summary.

Output: data/analysis/backtest_a_plus_b_{settled,eval}.{csv,txt}
"""

import csv
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(DATA, "analysis")
os.makedirs(OUT_DIR, exist_ok=True)

# Gate constants — copied from weather_predictor/predictor.py
EDGE_YES = 1.5
EDGE_NO = 1.05
MAX_ASK_YES = 0.50
MAX_ASK_NO = 0.95
MIN_PROB_YES = 0.15
MIN_PROB_NO = 0.85

# Bias policy timeline (ISO timestamps)
T_PER_KIND = "2026-05-15T11:26:35+00:00"
T_PER_CITY = "2026-05-18T03:06:35+00:00"
T_AB = "2026-05-25T07:39:18+00:00"

PER_KIND_EARLY = {"highest": -0.143, "lowest": 1.112}    # 5/15-5/18
PER_KIND_LATE = {"highest": -0.143, "lowest": 1.159}     # 5/18-5/25 (fallback)
PER_CITY_KIND = {                                          # 5/18-5/25 overrides
    "Atlanta|highest": 0.463, "Chicago|highest": -0.649,
    "Miami|highest": 1.785, "Miami|lowest": 1.617,
    "NYC|highest": 0.184, "NYC|lowest": 0.628,
}


def cdf(x, mu, s):
    return 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))


def p_yes_for_bucket(bucket, mu, sigma):
    if bucket.startswith("<="):
        return cdf(float(bucket[2:]), mu, sigma)
    if bucket.startswith(">="):
        return 1 - cdf(float(bucket[2:]), mu, sigma)
    lo_s, _, hi_s = bucket.partition("-")
    return cdf(float(hi_s), mu, sigma) - cdf(float(lo_s), mu, sigma)


def in_bucket(extreme, bucket):
    if bucket.startswith("<="):
        return extreme < float(bucket[2:])
    if bucket.startswith(">="):
        return extreme >= float(bucket[2:])
    lo_s, _, hi_s = bucket.partition("-")
    return float(lo_s) <= extreme < float(hi_s)


def bias_at(ts, city, kind):
    if ts < T_PER_KIND:
        return 0.0
    if ts < T_PER_CITY:
        return PER_KIND_EARLY.get(kind, 0.0)
    if ts < T_AB:
        cb = PER_CITY_KIND.get(f"{city}|{kind}")
        return cb if cb is not None else PER_KIND_LATE.get(kind, 0.0)
    return 0.0


def ab_sigma(recorded_sigma, scenario):
    """σ proxy under two scenarios bracketing the unknown ensemble_std.
      'tight'  = production default when ensemble agrees: 1.0
      'loose'  = upper-bound: max(recorded_sigma, 1.0)
    Reality is between."""
    if scenario == "tight":
        return 1.0
    return max(recorded_sigma, 1.0)


CLAMP_LO = 0.05
CLAMP_HI = 0.95
MAX_ASK_NO_NEW = 0.85   # target: block 0.86-0.95 NO entries (lowest blowup band)


def ab_would_enter(side, raw_t_hat, sigma_proxy, bucket, ask):
    """Apply A+B gate. Returns (would_enter, our_prob_clamped, edge, reason)."""
    if raw_t_hat is None or sigma_proxy is None or ask is None or ask <= 0:
        return False, None, None, "missing inputs"
    raw_p_yes = p_yes_for_bucket(bucket, raw_t_hat, sigma_proxy)
    clamped_yes = max(CLAMP_LO, min(CLAMP_HI, raw_p_yes))
    our_prob = clamped_yes if side == "YES" else 1.0 - clamped_yes
    max_ask = MAX_ASK_YES if side == "YES" else MAX_ASK_NO_NEW
    min_prob = MIN_PROB_YES if side == "YES" else MIN_PROB_NO
    edge_thresh = EDGE_YES if side == "YES" else EDGE_NO
    if ask > max_ask:
        return False, our_prob, None, f"ask>{max_ask}"
    if our_prob < min_prob:
        return False, our_prob, None, f"our_prob<{min_prob}"
    edge = our_prob / ask
    if edge < edge_thresh:
        return False, our_prob, edge, f"edge<{edge_thresh}"
    return True, our_prob, edge, "OK"


def load_iem_actuals():
    out = {}
    path = os.path.join(DATA, "nws_actuals.csv")
    for r in csv.DictReader(open(path)):
        if r.get("status") != "ok":
            continue
        try:
            out[(r["city"], r["local_date"])] = (
                float(r["actual_low"]), float(r["actual_high"])
            )
        except Exception:
            continue
    return out


def settle_pnl(side, kind, bucket, cost, shares, actual_extreme):
    """Compute hold-to-resolve PnL given actual extreme."""
    yes_won = in_bucket(actual_extreme, bucket)
    won = yes_won if side == "YES" else (not yes_won)
    payout = shares if won else 0.0
    return payout - cost, won


# ──────────────────────────────────────────────────────────────────────────────
# Pass (a): paper_settled
# ──────────────────────────────────────────────────────────────────────────────

def run_settled(actuals):
    src = os.path.join(DATA, "paper_settled.csv")
    rows = list(csv.DictReader(open(src)))
    summaries = {}
    out_rows = []
    for scenario in ("tight", "loose"):
        n_total = 0; n_eval = 0; n_old_enter = 0
        n_new_enter = 0; n_blocked_by_ab = 0
        blocked_pnl = 0.0; kept_pnl = 0.0
        by_reason = defaultdict(int)
        by_city_kind = defaultdict(lambda: [0, 0, 0, 0.0, 0.0])
        for r in rows:
            n_total += 1; n_old_enter += 1
            ts = r.get("ts_opened") or ""
            if not ts or not r.get("t_hat_entry") or not r.get("sigma_entry"):
                continue
            try:
                recorded_t = float(r["t_hat_entry"])
                recorded_sigma = float(r["sigma_entry"])
                ask = float(r["entry_price"])
            except Exception:
                continue
            city, kind, side, bucket = r["city"], r["kind"], r["side"], r["bucket"]
            raw_t = recorded_t - bias_at(ts, city, kind)
            sigma_proxy = ab_sigma(recorded_sigma, scenario)
            n_eval += 1
            will, our_p, edge, reason = ab_would_enter(side, raw_t, sigma_proxy, bucket, ask)
            actual_pnl = float(r["pnl_usd"])
            bk = by_city_kind[(city, kind)]
            bk[0] += 1
            if will:
                n_new_enter += 1; bk[1] += 1; kept_pnl += actual_pnl; bk[4] += actual_pnl
            else:
                n_blocked_by_ab += 1; bk[2] += 1; blocked_pnl += actual_pnl; bk[3] += actual_pnl
                by_reason[reason] += 1
            if scenario == "tight":
                out_rows.append({
                    "ts_opened": ts, "city": city, "kind": kind,
                    "local_date": r["local_date"], "bucket": bucket, "side": side,
                    "entry_price": f"{ask:.4f}", "cost_usd": r["cost_usd"],
                    "recorded_t_hat": f"{recorded_t:.2f}", "recorded_sigma": f"{recorded_sigma:.2f}",
                    "raw_t_hat": f"{raw_t:.2f}", "ab_sigma_tight": "1.00",
                    "ab_our_prob_tight": f"{our_p:.4f}" if our_p is not None else "",
                    "ab_edge_tight": f"{edge:.3f}" if edge is not None else "",
                    "ab_would_enter_tight": "1" if will else "0",
                    "ab_block_reason_tight": reason if not will else "",
                    "exit_kind": r["exit_kind"], "actual_pnl": f"{actual_pnl:+.2f}",
                })
        summaries[scenario] = (n_total, n_eval, n_old_enter, n_new_enter,
                                n_blocked_by_ab, blocked_pnl, kept_pnl,
                                dict(by_reason), dict(by_city_kind))

    out_csv = os.path.join(OUT_DIR, "backtest_a_plus_b_settled.csv")
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    lines = []
    lines.append("== Pass (a) paper_settled backtest — two σ scenarios ==")
    lines.append("σ scenario 'tight' = 1.0 (ensemble-agrees, what production A+B usually does)")
    lines.append("σ scenario 'loose' = max(recorded_sigma, 1.0) (ensemble≈recorded in-sample, upper bound)")
    lines.append("Reality is between; tight blocks more, loose blocks fewer.")
    lines.append("")
    for scenario in ("tight", "loose"):
        (n_total, n_eval, n_old_enter, n_new_enter, n_blocked_by_ab,
         blocked_pnl, kept_pnl, by_reason, by_city_kind) = summaries[scenario]
        lines.append(f"--- σ = {scenario} ---")
        lines.append(f"rows: {n_total}  evaluable: {n_eval}")
        lines.append(f"old entered: {n_old_enter}  A+B enters: {n_new_enter}  blocked: {n_blocked_by_ab}")
        lines.append(f"  kept (both)     : ${kept_pnl:+9.2f}  n={n_new_enter}")
        lines.append(f"  blocked by A+B  : ${blocked_pnl:+9.2f}  n={n_blocked_by_ab}")
        lines.append(f"  net change      : ${-blocked_pnl:+9.2f}  (A+B forfeits this; positive = forfeit profit)")
        lines.append("  block reasons:")
        for k, v in sorted(by_reason.items(), key=lambda x: -x[1]):
            lines.append(f"    {k:28s} n={v}")
        lines.append("  by city × kind (old / new / blocked / blk_pnl / kept_pnl):")
        for (c, k), v in sorted(by_city_kind.items(), key=lambda x: -x[1][3]):
            lines.append(f"    {c:8s} {k:7s}  old={v[0]:3d}  new={v[1]:3d}  blk={v[2]:3d}  "
                         f"blk_pnl=${v[3]:+8.2f}  kept_pnl=${v[4]:+8.2f}")
        lines.append("")

    out_txt = os.path.join(OUT_DIR, "backtest_a_plus_b_settled.txt")
    with open(out_txt, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n-> {out_csv}\n-> {out_txt}")


# ──────────────────────────────────────────────────────────────────────────────
# Pass (b): 9.5-day EVAL window
# ──────────────────────────────────────────────────────────────────────────────

EVAL_SOURCES = [
    os.path.join(DATA, "archive_20260421", "paper_decisions.csv"),
    os.path.join(DATA, "archive_20260421", "paper_decisions_lateflush.csv"),
    os.path.join(DATA, "archive_20260421_v2", "paper_decisions.csv"),
    os.path.join(DATA, "paper_decisions.csv.1"),
    os.path.join(DATA, "paper_decisions.csv"),
]


def run_eval(actuals):
    """Dedup by (city, kind, local_date, market_slug, bucket, side) cohort.
    A cohort is 'old-entered' if ANY EVAL row has action=ENTER for it.
    A cohort is 'ab-would-enter' (for a scenario) if ANY EVAL row in it
    passes the A+B gate under that scenario."""
    # cohort -> {old_entered: bool, ab_entered_tight: bool, ab_entered_loose: bool,
    #            first_ts, first_ask, last_ask, city, kind, local_date, bucket, side}
    cohorts = {}
    n_total = 0; n_eval_action = 0; n_old_enter_action = 0
    by_reason = {"tight": defaultdict(int), "loose": defaultdict(int)}

    for src in EVAL_SOURCES:
        if not os.path.exists(src):
            print(f"  (skip missing) {src}", file=sys.stderr)
            continue
        with open(src) as fh:
            rd = csv.DictReader(fh)
            for r in rd:
                n_total += 1
                action = r.get("action", "")
                if action == "EVAL": n_eval_action += 1
                if action == "ENTER": n_old_enter_action += 1
                if not r.get("t_hat") or not r.get("sigma") or not r.get("ask"):
                    continue
                try:
                    recorded_t = float(r["t_hat"])
                    recorded_sigma = float(r["sigma"])
                    ask = float(r["ask"])
                except Exception:
                    continue
                ts = r["ts"]
                city, kind, side = r["city"], r["kind"], r["side"]
                bucket = r["bucket"]; ms = r.get("market_slug", "")
                ld = r["local_date"]
                key = (city, kind, ld, ms, bucket, side)
                if key not in cohorts:
                    cohorts[key] = {
                        "city": city, "kind": kind, "local_date": ld,
                        "market_slug": ms, "bucket": bucket, "side": side,
                        "old_entered": False, "tight": False, "loose": False,
                        "first_ts": ts, "best_ask": ask, "best_t_hat": recorded_t,
                        "best_sigma": recorded_sigma,
                    }
                c = cohorts[key]
                if action == "ENTER":
                    c["old_entered"] = True
                raw_t = recorded_t - bias_at(ts, city, kind)
                for sc in ("tight", "loose"):
                    sigma_proxy = ab_sigma(recorded_sigma, sc)
                    will, _, _, reason = ab_would_enter(
                        side, raw_t, sigma_proxy, bucket, ask
                    )
                    if will and not c[sc]:
                        c[sc] = True
                        # snapshot at first-pass moment
                        c[f"{sc}_ts"] = ts
                        c[f"{sc}_ask"] = ask
                    if not will:
                        by_reason[sc][reason] += 1

    # Score cohorts that resolved via IEM
    def score(scenario):
        n_both = n_only_old = n_only_ab = n_neither = 0
        pnl_both = pnl_only_old = pnl_only_ab = 0.0
        resolved = 0
        for c in cohorts.values():
            old = c["old_entered"]
            ab = c[scenario]
            k = (c["city"], c["local_date"])
            if not (old or ab):
                n_neither += 1; continue
            if k not in actuals:
                continue
            resolved += 1
            ext = actuals[k][1] if c["kind"] == "highest" else actuals[k][0]
            # use first-enter ask of whichever side took it
            ask = c.get(f"{scenario}_ask") if ab else c["best_ask"]
            cost = 150.0
            shares = cost / ask
            pnl, _ = settle_pnl(c["side"], c["kind"], c["bucket"], cost, shares, ext)
            if old and ab: n_both += 1; pnl_both += pnl
            elif old: n_only_old += 1; pnl_only_old += pnl
            elif ab:  n_only_ab += 1;  pnl_only_ab += pnl
        return n_both, n_only_old, n_only_ab, pnl_both, pnl_only_old, pnl_only_ab, resolved

    n_cohorts = len(cohorts)
    n_old_cohorts = sum(1 for c in cohorts.values() if c["old_entered"])
    n_ab_tight = sum(1 for c in cohorts.values() if c["tight"])
    n_ab_loose = sum(1 for c in cohorts.values() if c["loose"])

    lines = []
    lines.append("== Pass (b) 9.5-day EVAL backtest — deduped by cohort ==")
    lines.append(f"raw rows scanned: {n_total}  (EVAL={n_eval_action}, ENTER={n_old_enter_action})")
    lines.append(f"unique cohorts (city,kind,date,market,bucket,side): {n_cohorts}")
    lines.append(f"  old-entered cohorts: {n_old_cohorts}")
    lines.append(f"  A+B would-enter (tight σ=1.0):  {n_ab_tight}")
    lines.append(f"  A+B would-enter (loose σ=recorded): {n_ab_loose}")
    lines.append("")
    for sc in ("tight", "loose"):
        n_both, n_only_old, n_only_ab, p_b, p_o, p_a, resolved = score(sc)
        lines.append(f"--- σ = {sc} — IEM-resolvable cohorts ($150 notional) ---")
        lines.append(f"  resolved: {resolved}")
        lines.append(f"  both enter  : n={n_both:5d}  PnL=${p_b:+10.2f}")
        lines.append(f"  only old    : n={n_only_old:5d}  PnL=${p_o:+10.2f}  (A+B avoids)")
        lines.append(f"  only A+B    : n={n_only_ab:5d}  PnL=${p_a:+10.2f}  (A+B adds)")
        lines.append(f"  delta vs old: ${(p_b + p_a) - (p_b + p_o):+10.2f}  "
                     f"(positive = A+B improves on the resolvable slice)")
        lines.append("  top A+B block reasons:")
        for k, v in sorted(by_reason[sc].items(), key=lambda x: -x[1])[:6]:
            lines.append(f"    {k:28s} n={v}")
        lines.append("")
    lines.append("Caveats:")
    lines.append("  - cost fixed at $150 / cohort (no Kelly, no bankroll, no portfolio caps).")
    lines.append("  - No exit simulation; all entries scored as hold-to-resolve.")
    lines.append("  - 9.5-day window (4/18-4/21 + 5/19-5/25); 4/22-5/18 EVAL was rotated out.")
    lines.append("  - 'tight' σ=1.0 mirrors production A+B when ensemble agrees; 'loose' uses")
    lines.append("    historical recorded σ as upper bound. Real outcomes lie between.")

    out_txt = os.path.join(OUT_DIR, "backtest_a_plus_b_eval.txt")
    with open(out_txt, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n-> {out_txt}")


if __name__ == "__main__":
    actuals = load_iem_actuals()
    print(f"IEM actuals loaded: {len(actuals)} (city, date) pairs\n")
    run_settled(actuals)
    print()
    run_eval(actuals)

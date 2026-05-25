"""Bias / calibration analysis joining paper_decisions × nws_actuals.

Design: audit/BIAS_ANALYSIS_PLAN.md (plan v5).
Output: data/analysis/{bias_per_city,calibration_bins}.csv + .txt + diagnostics.txt
"""

import argparse
import re
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = SCRIPT_DIR.parent / "data"

ENTRY_WINDOW = (0.5, 12.0)
BIN_WIDTH = 0.05
BOOTSTRAP_B = 1000
RNG_SEED = 0


def parse_bucket(text):
    text = (text or "").strip()
    m = re.match(r"^(\d+)-(\d+)$", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.match(r"^>=(\d+)$", text)
    if m:
        return float(m.group(1)), None
    m = re.match(r"^<=(\d+)$", text)
    if m:
        return None, float(m.group(1))
    return None


def yes_hit(a, lo, hi):
    if lo is not None and hi is not None:
        return lo <= a <= hi
    if lo is None and hi is not None:
        return a <= hi
    if hi is None and lo is not None:
        return a >= lo
    return None


def hit(a, lo, hi, side):
    yh = yes_hit(a, lo, hi)
    if yh is None:
        return None
    return bool(yh if side == "YES" else (not yh))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = data_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_csv = data_dir / "paper_decisions.csv"
    actuals_csv = data_dir / "nws_actuals.csv"

    con = duckdb.connect()

    print(f"loading {decisions_csv} ...", file=sys.stderr)
    con.execute(f"""
        CREATE TABLE raw AS
        SELECT * FROM read_csv_auto('{decisions_csv}', header=True, all_varchar=True)
    """)
    n_raw = con.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
    print(f"  raw rows: {n_raw:,}", file=sys.stderr)

    con.execute("""
        CREATE TABLE eval_rows AS
        SELECT
            ts, city, kind, local_date, market_slug, bucket, side,
            TRY_CAST(t_hat AS DOUBLE) AS t_hat,
            TRY_CAST(sigma AS DOUBLE) AS sigma,
            TRY_CAST(our_prob AS DOUBLE) AS our_prob,
            TRY_CAST(hours_to_close AS DOUBLE) AS hours_to_close,
            hours_to_close AS hours_to_close_str
        FROM raw
        WHERE action='EVAL'
          AND t_hat IS NOT NULL
          AND TRY_CAST(t_hat AS DOUBLE) IS NOT NULL
    """)
    n_eval = con.execute("SELECT COUNT(*) FROM eval_rows").fetchone()[0]
    print(f"  EVAL rows: {n_eval:,}", file=sys.stderr)

    con.execute(f"""
        CREATE TABLE lock_cohorts AS
        WITH lock_ts AS (
            SELECT city, kind, local_date, market_slug, MIN(ts) AS lock_ts
            FROM eval_rows
            WHERE hours_to_close BETWEEN {ENTRY_WINDOW[0]} AND {ENTRY_WINDOW[1]}
            GROUP BY city, kind, local_date, market_slug
        )
        SELECT
            l.city, l.kind, l.local_date, l.market_slug,
            l.lock_ts,
            e.hours_to_close_str AS lock_hours_str,
            e.t_hat AS t_hat_lock,
            e.sigma AS sigma_lock
        FROM lock_ts l
        JOIN eval_rows e USING (city, kind, local_date, market_slug)
        WHERE e.ts = l.lock_ts
    """)
    n_cohorts = con.execute("SELECT COUNT(*) FROM lock_cohorts").fetchone()[0]
    print(f"  lock_cohorts: {n_cohorts}", file=sys.stderr)

    con.execute("""
        CREATE TABLE cohort_rows AS
        SELECT
            e.city, e.kind, e.local_date, e.market_slug,
            e.bucket, e.side, e.t_hat, e.sigma, e.our_prob,
            e.hours_to_close_str
        FROM eval_rows e
        JOIN lock_cohorts c USING (city, kind, local_date, market_slug)
        WHERE c.lock_hours_str = e.hours_to_close_str
    """)
    n_cohort_rows = con.execute("SELECT COUNT(*) FROM cohort_rows").fetchone()[0]
    print(f"  cohort_rows: {n_cohort_rows:,}", file=sys.stderr)

    con.execute(f"""
        CREATE TABLE actuals_wide AS
        SELECT
            city, local_date,
            TRY_CAST(actual_high AS DOUBLE) AS actual_high,
            TRY_CAST(actual_low AS DOUBLE) AS actual_low
        FROM read_csv_auto('{actuals_csv}', header=True, all_varchar=True)
        WHERE status='ok'
    """)
    con.execute("""
        CREATE TABLE actuals_long AS
        SELECT city, local_date, 'highest' AS kind, actual_high AS actual FROM actuals_wide
        UNION ALL
        SELECT city, local_date, 'lowest' AS kind, actual_low AS actual FROM actuals_wide
    """)
    n_actuals = con.execute("SELECT COUNT(*) FROM actuals_long").fetchone()[0]
    print(f"  actuals_long: {n_actuals}", file=sys.stderr)

    diagnostics = []

    # ── Bias ────────────────────────────────────────────────────────────────────
    bias_full = con.execute("""
        SELECT
            c.city, c.kind, c.local_date, c.market_slug,
            c.t_hat_lock, c.sigma_lock,
            a.actual,
            a.actual - c.t_hat_lock AS err
        FROM lock_cohorts c
        JOIN actuals_long a USING (city, kind, local_date)
    """).df()

    n_before_dedupe = len(bias_full)
    bias_dedup = (
        bias_full.sort_values("market_slug")
        .groupby(["city", "kind", "local_date"], as_index=False)
        .first()
    )
    n_dropped = n_before_dedupe - len(bias_dedup)
    diagnostics.append(f"bias dedupe: {n_before_dedupe} cohort rows → "
                       f"{len(bias_dedup)} after dedupe ({n_dropped} dropped)")

    def agg_bias(g):
        n = len(g)
        err = g["err"].to_numpy()
        mean_err = float(np.mean(err))
        std = float(np.std(err, ddof=1)) if n > 1 else 0.0
        if n > 1:
            t_mult = float(stats.t.ppf(0.975, n - 1))
            half = t_mult * std / np.sqrt(n)
        else:
            half = float("nan")
        return pd.Series({
            "n": n,
            "mean_err": mean_err,
            "median_err": float(np.median(err)),
            "std_err": std,
            "ci95_low": mean_err - half,
            "ci95_high": mean_err + half,
            "mean_t_hat": float(g["t_hat_lock"].mean()),
            "median_t_hat": float(g["t_hat_lock"].median()),
            "mean_actual": float(g["actual"].mean()),
            "median_actual": float(g["actual"].median()),
            "mean_sigma": float(g["sigma_lock"].mean()),
        })

    bias_agg = (
        bias_dedup.groupby(["city", "kind"], as_index=False)
        .apply(agg_bias, include_groups=False)
        .reset_index(drop=True)
    )
    bias_agg = bias_agg.assign(_abs=bias_agg["mean_err"].abs()) \
                       .sort_values("_abs", ascending=False) \
                       .drop(columns=["_abs"]) \
                       .reset_index(drop=True)

    bias_path = out_dir / "bias_per_city.csv"
    bias_agg.to_csv(bias_path, index=False, float_format="%.3f")
    print(f"  wrote {bias_path}", file=sys.stderr)

    # bias txt summary
    bias_txt_lines = ["# bias_per_city — sorted by |mean_err|"]
    bias_txt_lines.append(f"{'city':<12s} {'kind':<8s} {'n':>3s} "
                          f"{'mean':>7s} {'med':>7s} {'std':>6s} "
                          f"{'CI95':>16s} {'meanT':>6s} {'meanA':>6s}")
    for _, r in bias_agg.iterrows():
        flag = " *" if abs(r["mean_err"]) > 1.0 else ""
        bias_txt_lines.append(
            f"{r['city']:<12s} {r['kind']:<8s} {int(r['n']):>3d} "
            f"{r['mean_err']:>+7.2f} {r['median_err']:>+7.2f} {r['std_err']:>6.2f} "
            f"[{r['ci95_low']:>+5.2f},{r['ci95_high']:>+5.2f}] "
            f"{r['mean_t_hat']:>6.1f} {r['mean_actual']:>6.1f}{flag}"
        )
    (out_dir / "bias_per_city.txt").write_text("\n".join(bias_txt_lines) + "\n")

    # ── Calibration ─────────────────────────────────────────────────────────────
    calib_raw = con.execute("""
        SELECT
            cr.city, cr.kind, cr.local_date, cr.market_slug, cr.bucket, cr.side,
            cr.our_prob, a.actual
        FROM cohort_rows cr
        JOIN actuals_long a USING (city, kind, local_date)
        WHERE cr.our_prob IS NOT NULL
    """).df()

    parsed = calib_raw["bucket"].map(parse_bucket)
    calib_raw["bucket_lo"] = parsed.map(lambda p: p[0] if p else None)
    calib_raw["bucket_hi"] = parsed.map(lambda p: p[1] if p else None)
    calib_raw = calib_raw.dropna(subset=["actual"])
    calib_raw = calib_raw[parsed.notna()]
    n_unparseable = (~parsed.notna()).sum()
    if n_unparseable:
        diagnostics.append(f"calibration: {n_unparseable} rows with unparseable bucket dropped")

    # half-up rounding to match predictor settlement (math.floor(x+0.5));
    # pandas .round() is banker's (68.5->68) and would flip .5 boundary
    # actuals vs the live settlement model (A10 consistency)
    calib_raw["actual_int"] = np.floor(calib_raw["actual"] + 0.5).astype(int)
    calib_raw["hit"] = calib_raw.apply(
        lambda r: hit(r["actual_int"], r["bucket_lo"], r["bucket_hi"], r["side"]),
        axis=1,
    )
    calib_raw = calib_raw.dropna(subset=["hit"]).copy()
    calib_raw["hit"] = calib_raw["hit"].astype(int)

    bin_edges = np.arange(0.0, 1.0 + 1e-9, BIN_WIDTH)
    calib_raw["bin_left"] = np.clip(
        np.floor(calib_raw["our_prob"] / BIN_WIDTH).astype(int), 0, len(bin_edges) - 2
    ) * BIN_WIDTH

    calib_raw["cluster_id"] = (
        calib_raw["city"] + "|" + calib_raw["kind"] + "|"
        + calib_raw["local_date"] + "|" + calib_raw["market_slug"]
    )

    # Per-bin point estimates
    calib_agg = (
        calib_raw.groupby("bin_left", as_index=False)
        .agg(n=("hit", "count"),
             our_prob_mean=("our_prob", "mean"),
             hit_rate=("hit", "mean"))
    )

    # Cluster bootstrap
    pivot = (
        calib_raw.groupby(["cluster_id", "bin_left"])
        .agg(hits=("hit", "sum"), n=("hit", "count"))
        .reset_index()
    )
    bin_lefts = sorted(calib_raw["bin_left"].unique())
    cluster_ids = sorted(calib_raw["cluster_id"].unique())
    bin_idx = {bl: i for i, bl in enumerate(bin_lefts)}
    cid_idx = {c: i for i, c in enumerate(cluster_ids)}

    n_c = len(cluster_ids)
    n_b = len(bin_lefts)
    hits = np.zeros((n_c, n_b), dtype=np.int64)
    counts = np.zeros((n_c, n_b), dtype=np.int64)
    for _, r in pivot.iterrows():
        ci = cid_idx[r["cluster_id"]]
        bi = bin_idx[r["bin_left"]]
        hits[ci, bi] = r["hits"]
        counts[ci, bi] = r["n"]

    rng = np.random.default_rng(RNG_SEED)
    boot = np.zeros((BOOTSTRAP_B, n_b))
    for b in range(BOOTSTRAP_B):
        idx = rng.integers(0, n_c, size=n_c)
        h = hits[idx].sum(axis=0)
        c = counts[idx].sum(axis=0)
        boot[b] = np.where(c > 0, h / c, np.nan)

    ci_low = np.nanquantile(boot, 0.025, axis=0)
    ci_high = np.nanquantile(boot, 0.975, axis=0)
    ci_df = pd.DataFrame({"bin_left": bin_lefts, "ci95_low": ci_low, "ci95_high": ci_high})
    calib_agg = calib_agg.merge(ci_df, on="bin_left", how="left")
    calib_agg["bin_right"] = calib_agg["bin_left"] + BIN_WIDTH
    calib_agg = calib_agg[
        ["bin_left", "bin_right", "n", "our_prob_mean", "hit_rate", "ci95_low", "ci95_high"]
    ].sort_values("bin_left").reset_index(drop=True)

    calib_path = out_dir / "calibration_bins.csv"
    calib_agg.to_csv(calib_path, index=False, float_format="%.4f")
    print(f"  wrote {calib_path}", file=sys.stderr)

    calib_txt = ["# calibration_bins — cluster bootstrap CI (B=1000, "
                 "cluster=(city,kind,local_date,market_slug))"]
    calib_txt.append(f"{'bin':>11s} {'n':>5s} {'our_p':>7s} {'hit':>7s} "
                     f"{'CI95':>17s}")
    for _, r in calib_agg.iterrows():
        calib_txt.append(
            f"[{r['bin_left']:.2f},{r['bin_right']:.2f}) {int(r['n']):>5d} "
            f"{r['our_prob_mean']:>7.4f} {r['hit_rate']:>7.4f} "
            f"[{r['ci95_low']:>+5.4f},{r['ci95_high']:>+5.4f}]"
        )
    (out_dir / "calibration.txt").write_text("\n".join(calib_txt) + "\n")

    # ── Diagnostics ─────────────────────────────────────────────────────────────
    # Coverage
    expected = con.execute("SELECT DISTINCT city, kind, local_date FROM actuals_long").df()
    found = con.execute("SELECT DISTINCT city, kind, local_date FROM lock_cohorts").df()
    expected["key"] = expected["city"] + "|" + expected["kind"] + "|" + expected["local_date"]
    found["key"] = found["city"] + "|" + found["kind"] + "|" + found["local_date"]

    missing = expected[~expected["key"].isin(found["key"])][["city", "kind", "local_date"]]
    diagnostics.append(f"\ncoverage: expected={len(expected)}  "
                       f"found={len(found)}  missing={len(missing)} "
                       f"({100.0 * len(found) / len(expected):.1f}%)")

    cls_a = cls_b = cls_c = 0
    if len(missing):
        for _, row in missing.iterrows():
            stats_row = con.execute(
                "SELECT MIN(hours_to_close) AS mn, MAX(hours_to_close) AS mx "
                "FROM eval_rows WHERE city=? AND kind=? AND local_date=?",
                [row["city"], row["kind"], row["local_date"]],
            ).fetchone()
            if stats_row is None or stats_row[0] is None:
                cls_c += 1
            else:
                mn, mx = stats_row
                if mx < ENTRY_WINDOW[0]:
                    cls_a += 1
                elif mn > ENTRY_WINDOW[1]:
                    cls_b += 1
                else:
                    cls_c += 1
    diagnostics.append(f"  (a) all hours_to_close < {ENTRY_WINDOW[0]}: {cls_a}")
    diagnostics.append(f"  (b) all hours_to_close > {ENTRY_WINDOW[1]}: {cls_b}")
    diagnostics.append(f"  (c) other (no rows / predictor gap): {cls_c}")

    # IEM anomaly flags
    actuals_df = con.execute(
        "SELECT city, local_date, actual_high, actual_low FROM actuals_wide ORDER BY city, local_date"
    ).df()
    diagnostics.append("\nIEM anomaly flags:")
    flagged = []
    for city, g in actuals_df.groupby("city"):
        g = g.reset_index(drop=True)
        for i, r in g.iterrows():
            reasons = []
            if abs(r["actual_high"] - r["actual_low"]) < 5:
                reasons.append(f"|hi-lo|={r['actual_high'] - r['actual_low']:.1f}<5")
            if i > 0:
                prev = g.iloc[i - 1]
                if abs(r["actual_high"] - prev["actual_high"]) > 25:
                    reasons.append(f"hi jump {prev['actual_high']:.1f}→{r['actual_high']:.1f}")
                if abs(r["actual_low"] - prev["actual_low"]) > 25:
                    reasons.append(f"lo jump {prev['actual_low']:.1f}→{r['actual_low']:.1f}")
            if reasons:
                flagged.append(f"  {city} {r['local_date']}: {'; '.join(reasons)}")
    if flagged:
        diagnostics.extend(flagged)
    else:
        diagnostics.append("  (none)")

    # Borderline cohorts (±1°F shift hit-flip)
    diagnostics.append("\nborderline cohorts (CF6 ±1°F flips any bucket hit):")
    n_total = 0
    n_border = 0
    for cid, g in calib_raw.groupby("cluster_id"):
        n_total += 1
        a0 = int(g["actual_int"].iloc[0])
        flipped = False
        for da in (-1, 1):
            for _, r in g.iterrows():
                h0 = hit(a0, r["bucket_lo"], r["bucket_hi"], r["side"])
                ha = hit(a0 + da, r["bucket_lo"], r["bucket_hi"], r["side"])
                if h0 != ha:
                    flipped = True
                    break
            if flipped:
                break
        if flipped:
            n_border += 1
    pct = 100.0 * n_border / n_total if n_total else 0.0
    diagnostics.append(f"  {n_border}/{n_total} ({pct:.1f}%)")

    diag_path = out_dir / "diagnostics.txt"
    diag_path.write_text("\n".join(diagnostics) + "\n")
    print(f"  wrote {diag_path}", file=sys.stderr)

    print("\n" + "\n".join(diagnostics))


if __name__ == "__main__":
    main()

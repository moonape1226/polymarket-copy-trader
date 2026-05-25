# Review: BIAS_ANALYSIS_PLAN.md

## Round 1 Findings

1. High: `kind` join is wrong. The plan uses `high` / `low`, but `data/paper_decisions.csv` uses `highest` / `lowest`, so the join will produce zero rows unless normalized. See `audit/BIAS_ANALYSIS_PLAN.md:28` and `data/paper_decisions.csv:1-2`.

2. High: Sampling one row per `(city, local_date, kind)` loses bucket-specific `our_prob`. Bias needs one forecast snapshot, but calibration needs all bucket/side rows from that snapshot. See `audit/BIAS_ANALYSIS_PLAN.md:40-49`, `audit/BIAS_ANALYSIS_PLAN.md:76-82`, and `weather_predictor/predictor.py:799-804`.

3. Medium: 24h is not the actual decision timing. Predictor entry window is `0.5-12h`, and current `22.44h` rows are outside entry. See `audit/BIAS_ANALYSIS_PLAN.md:45-49`, `weather_predictor/predictor.py:55`, and `data/paper_decisions.csv:2`.

4. Medium: Wilson CI treats bucket and YES/NO rows as independent, but rows share the same actual, and YES/NO are complements. The confidence interval will be too tight. See `audit/BIAS_ANALYSIS_PLAN.md:83-91`.

## Round 1 Proposed Fixes (agreed)

- **kind 命名**：改成 `highest` / `lowest`；nws_actuals unpivot 也對齊這兩個值。
- **兩階段取樣**：
  - bias：每個 `(city, local_date, kind)` 抓 entry window 內最早 ts 的「任一 row」（t_hat 對該 ts 的所有 bucket 一致）。
  - calibration：抓**同一個 ts** 的所有 bucket × side rows，保留每筆自己的 `our_prob`。
- **進場時點**：取 `hours_to_close ∈ [0.5, 12.0]` 內最早 EVAL row；若該 (city, local_date, kind) 區間內無 row，標 `no_lock_row` 並從 bias 表剔除（stdout 印計數）。
- **CI**：改用 cluster bootstrap，cluster = `(city, local_date, kind)`；單 cluster 約 60 rows（30 bucket × 2 side），共用 actual 且 YES/NO 互補，不 cluster 會嚴重低估 CI。

## Round 2 Findings

5. High: Bucket boundary vs IEM actual rounding mismatch. Buckets are integer °F closed intervals like `32-33` / `34-35` (`weather_predictor/predictor.py:663-668`), adjacent and non-overlapping. IEM `actual_high` is a float (e.g., 36.5°F). The plan's hit rule `bucket_lo ≤ actual ≤ bucket_hi` will mark a 33.5°F reading as miss for both `32-33` and `34-35`. Polymarket settles on the rounded NWS reading. See `audit/BIAS_ANALYSIS_PLAN.md:80-81`. **Fix:** `round(actual)` before the comparison; ideally double-confirm Polymarket's settlement source. If unsure, run both rules and report divergence.

6. Medium: Section 6 uses `1.96 · std/√n` for the bias CI, but n≈14 requires Student-t (df=13, t₀.₀₂₅=2.16). CI is ~10% wider; small-sample correction is non-optional. See `audit/BIAS_ANALYSIS_PLAN.md:68`. **Fix:** use `scipy.stats.t.ppf(0.975, n-1)` or hardcode the t-multiplier per n.

7. Medium: No filter on `action != EVAL` or empty `t_hat`. paper_decisions.csv has 3.51M rows mixing EVAL / HOLD / ENTER / EXIT; only EVAL carries valid `t_hat` / `our_prob`. The plan never states the filter, so a literal implementation would pull HOLD rows with empty `t_hat` strings into the join. **Fix:** explicit `WHERE action='EVAL' AND t_hat <> ''` filter at the DuckDB query level.

8. Low: Open-tail buckets `>=NN` / `<=NN` not addressed. The hit-rule pseudocode only covers `lo-hi` form. Plan should specify: `>=78` + `actual=82` → YES hit; `<=29` + `actual=25` → YES hit. See `audit/BIAS_ANALYSIS_PLAN.md:78-81`.

9. Low: Section 11's "HAC 等 n≥90" rationale doesn't map to per-city CI. n is per-city per-kind and stays ~14 for many weeks; HAC won't help here regardless of pooled-n growth. Plan should clarify: per-city uses Student-t small-sample CI; HAC applies only to the pooled all-cities daily error series in DESIGN.md §4.

10. Low: KHOU 4/23 `lo=36.5°F` flagged but no concrete sanity check. **Fix:** flag rows where `(actual_high − actual_low) < 5°F` or where adjacent-day `|actual − neighbor_actual| > 25°F` for inspection.

## Round 3 Findings (against updated plan)

11. **High: §5.3 "lock_ts 那個 ts 下的全部 rows" 會只剩 1 row。** `weather_predictor/predictor.py:808` 對每個 `(bucket, side)` 都呼叫 `datetime.now().isoformat()`，且 line 806 `get_orderbook` 帶來 100–300ms 延遲，所以單一 cycle 內 ~60 個 rows 的 `ts` 兩兩不同（毫秒級散開）。用 `WHERE ts = lock_ts` 過濾只會抓回 1 row，calibration 崩掉。
    - **Fix**：cohort key 改用 `(city, kind, local_date, market_slug, hours_to_close)`。`hours` 在 `predictor.py:730` 每個 market 每 cycle 算一次、line 818 格式化到 2 位小數，所以同 cycle 同 market 的所有 rows 共用相同 `hours_to_close` 字串。lock cohort = lock-row 的 `(city, kind, local_date, market_slug)` × `hours_to_close = lock-row.hours_to_close` 全部 rows。
    - 副作用：§5.1 的 lock 選法應改為「entry window 內 `min(ts)` 那一 row 對應的 `hours_to_close`」，而非 ts 本身。

12. **Medium: §7 `round(actual)` 仍與 Polymarket 結算源有 ±1°F 落差。** Polymarket 用 NWS 每日 climate report（CF6 form）結算，CF6 的 daily high/low 來自 NWS 內部彙整，未必等於 IEM ASOS 5-min readings 的 `round(max(...))`。對接近 bucket 邊界的日子，YES/NO hit 判斷可能反向。
    - **Fix**：在 §7 與 §3 的 actual 來源處註明此差距；diagnostics.txt 加印「本 IEM 取整 vs 該市相鄰 bucket 邊界」距離 ≤1°F 的 (city, local_date, kind) 計數，使用者能評估有多少筆受影響。

13. **Low: §4.4 `WHERE t_hat <> ''` 假設欄位型別為 VARCHAR。** DuckDB `read_csv` 預設 type-infer，若該欄推論為 DOUBLE，空字串會轉成 NULL，`<> ''` 會把所有 NULL 一併排除，看似沒事但語意改變；若推論為 VARCHAR 才如預期。
    - **Fix**：強制指定 `read_csv(..., types={'t_hat': 'VARCHAR'})` 或改寫為 `WHERE action='EVAL' AND t_hat IS NOT NULL AND TRY_CAST(t_hat AS DOUBLE) IS NOT NULL`。

14. **Low: §6 `mean_t_hat` / `mean_actual` 對 KHOU 4/23 異常值不穩。** n=14 時單筆 36.5°F 偏離可把 mean 拉開 ~3°F；既然 §9.2 已 flag 但不剔除，diagnostic stats 應再加 `median_t_hat` / `median_actual` 欄，輸出時兩者並列。

15. **Low: §9.1 lock_ts 覆蓋率成因混淆。** 「< 90% 表示窗太緊」過度簡化。其他成因：(a) 市場開盤晚於 close − 12h，(b) `paper_decisions.csv` 開始記錄那天的早盤市場已過窗，(c) predictor 重啟期間漏記。
    - **Fix**：diagnostics.txt 拆「市場開盤晚於 entry window 起點」、「entry window 落於 paper_decisions 記錄前」、「其他（含 predictor 中斷）」三類計數。

## Round 4 Findings (against plan v3)

16. **Medium: §9.6 borderline metric 邏輯錯誤，等同永遠成立。** bucket 寬度 2°F（`32-33` / `34-35` / ...），任意整數 `round(actual)` 與最近 bucket 邊界的距離恆 ≤ 1°F，門檻無篩選力，會把 100% cohort 都標 borderline。
    - **Fix**：改為「對每個 cohort，分別用 `round(actual)` 與 `round(actual) ± 1` 各跑一次所有 bucket 的 YES/NO hit；若任一 bucket 的 hit 結果不一致 → 該 cohort 標 borderline」。borderline 比例反映 CF6 ±1°F 抖動會翻轉多少校準訊號，才是有意義的指標。

17. **Low: §7 第一段仍用 `lock_ts` 字眼**（`audit/BIAS_ANALYSIS_PLAN.md:142`：「lock_ts × 所有 bucket × side」）。Round 3 已把核心改為 `lock_cohort`，此處是漏改的 stale wording。

18. **Low: §3 對 `actual_*` 的描述不精確。** `audit/backfill_actuals.py:130-131` 是 `round(max(temps), 2)`（2 位小數），非整數。整數 rounding 在 §7 hit 判斷時才做。
    - **Fix**：§3 改寫為「max/min over IEM 5-min readings，以 2-decimal precision 存檔；整數結算 rounding 於 §7 進行」，避免讀者誤以為已是整數。

19. **Low: §9.1 三分類定義不準確。**
    - (a)「市場開盤晚於 entry window 起點」字面上指 `hours_to_close < 12.0` 開盤，但這仍可能落入 entry window 中段，不構成 no_lock。
    - (b)「entry window 落於 paper_decisions 記錄前」邏輯混淆——若 csv 中段才開始記錄，仍可能抓到 partial coverage cohort，不必然是 no_lock。
    - **Fix**：重寫三類定義，回扣到「no_lock = entry window 內 0 EVAL row」的成因：
      - (a) 市場全部 EVAL row 的 `hours_to_close < 0.5`（entry window 完全錯過）
      - (b) csv 最早記錄時間 > 該 market 的 entry window 結束點
      - (c) 其他（predictor 中斷、市場未被 discovery 抓到）

## Round 5 Findings (against plan v4)

20. **Low: 同一 `(city, kind, local_date)` 對應多個 `market_slug` 時，§5.2 會 double-count 該日。** §5.1 cohort key 含 `market_slug`，§5.2「每 cohort 取一筆」。若某 (city, kind, local_date) 對應 2+ 個 market（Polymarket 偶有同概念多市場），會產生 2+ 個 lock_cohort 各取一筆 bias row，但 `t_hat` 對該日該 kind 是同一個 forecast 值。§6 `GROUP BY (city, kind)` 不分 market_slug → 等於該日在 mean / median 中權重 ×N。
    - **Fix**：§5.2 加一行 dedupe 規則，「若某 (city, kind, local_date) 對應多個 lock_cohort，取 `min(market_slug)` 那組進 bias 表」。實務上 Polymarket 多半一個 (city, kind, local_date) = 一個 market，但 plan 應有防呆。
    - 副作用：§7 calibration 不必 dedupe — 多個 market 的 buckets 都是有效的校準訊號（cluster bootstrap 的 cluster 單位本來就是 lock_cohort 含 market_slug，互不重疊）。

## Notes

- Round 1 findings 1–4：plan v2 已吸收。
- Round 2 findings 5–10：plan v2 已吸收。
- Round 3 findings 11–15：plan v3 已吸收；#12 / #15 落地細節見 Round 4 #16 / #19。
- Round 4 findings 16–19：plan v4 已吸收。
- Round 5 finding 20：plan v4 仍未涵蓋；Low 等級防呆，可在實作時直接補上。
- 截至 plan v4，語意層核心邏輯（cohort key 不依賴 ts、兩階段取樣、Student-t / cluster bootstrap CI、rounding / 開放尾 / 異常值 / borderline diagnostic）均已收斂，可進入實作。
- Station mapping 一致：`weather_predictor/predictor.py:131-155` ↔ `audit/audit.py:50-61`；predictor comment 明示 station 對齊市場結算地點。
- No code changes were made during the review.

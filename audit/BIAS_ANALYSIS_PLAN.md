# Bias / Calibration 分析實作計劃

一次性分析腳本，回頭用 `data/nws_actuals.csv`（IEM 回填的真實高低溫）評估
`data/paper_decisions.csv` 累積的預測與決策。本文件描述設計，不含程式碼。

> 修訂紀錄：
> - **Round 1**：kind 欄位命名、兩階段取樣（bias vs calibration）、entry
>   window 用 `[0.5, 12.0]h`、cluster bootstrap CI 取代 Wilson。
> - **Round 2**：`round(actual)` 對齊 Polymarket 整數結算、bias CI 改用
>   Student-t（n≈14）、明確 `action='EVAL'` 過濾、開放尾 bucket hit 規則、
>   HAC 適用範圍澄清、KHOU 異常具體檢測規則。
> - **Round 3**：cohort key 改用 `(city, kind, local_date, market_slug,
>   hours_to_close)`（ts 因 per-row `datetime.now()` + `get_orderbook` 延遲
>   而散開，無法當 cohort）；註明 NWS CF6 vs IEM round-max 結算落差；t_hat
>   過濾改用 `TRY_CAST`；bias 表加 `median_*` 欄；lock 覆蓋率診斷拆三類成因。
> - **Round 4**：§9.6 borderline 改用「±1°F shift 後 hit 翻轉」判定（原距離
>   ≤1°F 在 2°F 寬整數 bucket 下恆成立，無篩選力）；§7 殘留 `lock_ts` 字眼
>   清掉；§3 actual 精度改寫為「2-decimal，整數 rounding 在 §7 才做」；§9.1
>   三分類定義回扣到「no_lock = entry window 內 0 EVAL row」。
> - **Round 5**：§5.2 加 dedupe（同 (city, kind, local_date) 多 market_slug
>   時只取 `min(market_slug)` 進 bias，避免該日權重 ×N）；§7 calibration
>   不必 dedupe（cluster bootstrap cluster 含 market_slug，本就獨立）。

## 1. 目標

兩張產出表：

- **`data/analysis/bias_per_city.csv`** — 解 Miami / Atlanta 等城市偏差疑問
- **`data/analysis/calibration_bins.csv`** — our_prob 校準曲線（vs 對角線）

腳本位置：`audit/analyze_bias.py`（一次性，不進 docker-compose）。

## 2. 工具

- **DuckDB**（CLI 或 Python binding）— 直查 880MB CSV，免轉檔，3M row
  group-by 約 30–60s
- pandas / matplotlib 僅在最終彙整與繪圖階段使用（可選）

## 3. 資料源

| File | Rows | Schema 重點 |
|---|---|---|
| `data/paper_decisions.csv` | 3.51M | `ts, city, kind, local_date, market_slug, bucket, side, t_hat, sigma, our_prob, bid, ask, edge_ratio, hours_to_close, action, reason` |
| `data/nws_actuals.csv` | 139 | `city, station, local_date, actual_high, actual_low, status, n_obs, ...` |

`paper_decisions.kind` ∈ {`highest`, `lowest`}（**不是 high/low**），分別對應
`nws_actuals.actual_high` / `actual_low`。

> **Actual 來源與精度**：`nws_actuals.actual_*` 是 IEM ASOS 5-min readings 取
> `max/min`，存檔為 **2-decimal precision**（`backfill_actuals.py:130-131`
> `round(..., 2)`），不是整數。整數結算的 rounding 於 §7 hit 判斷時才做。
>
> **與 Polymarket 結算源差異**：Polymarket 用 NWS CF6 daily climate report
> 結算，CF6 的 daily high/low 來自 NWS 內部彙整，未必等於 IEM ASOS readings
> 的 max/min。對接近 bucket 邊界的日子，YES/NO hit 判斷可能反向。本計劃
> §9.2 / §9.6 列出影響範圍診斷，但不修正——本分析定位為「IEM 視角的真實
> 值」，校準結果視為近似指標。

`paper_decisions.bucket` 格式有三種：

- `lo-hi`（雙端範圍，例如 `48-49`、`100-101`）
- `>=N`（開放上尾，例如 `>=92`）
- `<=N`（開放下尾，例如 `<=29`）

## 4. Join 結構

1. nws_actuals 先 unpivot 成長表：
   - `(city, local_date, kind="highest", actual=actual_high)`
   - `(city, local_date, kind="lowest",  actual=actual_low)`
2. Join key：`(city, local_date, kind)`
3. 僅保留 `nws_actuals.status = "ok"` 的行；status=incomplete 的日子整天剔除
4. **基礎過濾**（DuckDB query 層）：
   ```sql
   WHERE action='EVAL'
     AND t_hat IS NOT NULL
     AND TRY_CAST(t_hat AS DOUBLE) IS NOT NULL
   ```
   - 用 `TRY_CAST` 而非 `t_hat <> ''`：DuckDB type-infer 可能把 t_hat 推成
     DOUBLE（空字串自動變 NULL），`<> ''` 語意會與 VARCHAR 推論不一致
   - HOLD 雖然也有 t_hat，但會對已 ENTER 市場重複登錄；EVAL 是覆蓋所有市場
     的純評估列
   - ENTER / EXIT 是 transition 事件，不是 forecast snapshot

## 5. 兩階段取樣（修正核心）

> 修正前的「一筆代表 row」取法會把同一 snapshot 下不同 bucket 的 `our_prob`
> 平均掉，calibration 曲線會被毀。bias 與 calibration 的取樣需求其實不同。

### 5.1 鎖定 cohort：lock_cohort

> **不能用 `ts` 作 cohort**：`predictor.py:808` 對每 row 重算
> `datetime.now().isoformat()`，且 `get_orderbook` (line 806) 帶 100–300ms 網路
> 延遲，所以同 cycle 同 market 的 ~60 rows 會有兩兩不同的 ts（毫秒級散開）。
> 但 `predictor.py:729-730` 的 `hours_to_close` 在進入 bucket/side 迴圈前算
> 一次、line 818 格式化到 `.2f`，**同 cycle 同 market 所有 rows 共用相同字串**。

針對每個 `(city, local_date, kind, market_slug)`，從 EVAL row 中找：

```
hours_to_close ∈ [0.5, 12.0]   # predictor.py:55 的 ENTRY_WINDOW_HOURS
↓
取 min(ts) 那一筆 row 對應的 hours_to_close 字串
↓
lock_cohort = (city, kind, local_date, market_slug, hours_to_close)
```

語意：**預測值首次進入「實際會下單」的時間窗那一個 evaluation cycle**，
最貼近 predictor 真實決策瞬間，且能用 cohort key 抓回該 cycle 全部 rows。

> 退路：若該 (city, local_date, kind, market_slug) 在 entry window 內無任何
> EVAL row，標 `no_lock_row`，從 bias 與 calibration 兩張表都剔除；
> diagnostics.txt 拆三類成因（見 §9.1）。

### 5.2 bias 用：lock-cohort 取一筆（含 dedupe）

從 `lock_cohort` 對應的所有 rows 中任取一行（同 cohort 內 `t_hat` / `sigma`
對所有 bucket / side 都相同）→ 取 `t_hat_lock` → join `actual` →
計 `err = actual − t_hat_lock`。

> **Dedupe**：`t_hat` 在 `predictor.py:730` 上是基於 `(city, kind, local_date)`
> 的 observed + forecast 算出，與 `market_slug` 無關。若同一 (city, kind,
> local_date) 對應多個 lock_cohort（多 market），它們共用同一個 t_hat，全部
> 留下會在 §6 GROUP BY (city, kind) 階段把該日權重放大 ×N。
>
> 規則：每個 (city, kind, local_date) 只取 `min(market_slug)` 對應的
> lock_cohort 進 bias 表；其他丟棄。實務上 Polymarket 多半一日一市場，這條
> 純屬防呆；diagnostics.txt 印「dedupe 丟棄」cohort 計數。

### 5.3 calibration 用：lock_cohort 全部 rows

`lock_cohort` 對應的**全部 rows**（每個 bucket × YES/NO 各一行），保留
個別 `our_prob` 與 `bucket`。每行用該 row 自己的 `(bucket, side, our_prob)` 對
`actual` 判 hit/miss。

> **不 dedupe**：calibration 的 cluster 單位（§7.1）就是 lock_cohort 含
> market_slug，多 market 之間互不重疊；多 market 各自的 buckets 都是有效校準
> 訊號，全部保留。

## 6. bias_per_city.csv

對每個 `(city, kind)` 聚合（先不分 side，因 5.2 是 lock-row 一筆，YES/NO 共用
同一 t_hat）：

```
city, kind, n,
mean_err, median_err, std_err,
ci95_low, ci95_high,
mean_t_hat, median_t_hat,
mean_actual, median_actual,
mean_sigma
```

- `err = actual − t_hat_lock`（正值 = 預測偏冷）
- **CI 用 Student-t**：`ci95 = mean ± t₀.₀₂₅(df=n-1) · std / √n`
  - n=14 時 t=2.16（vs normal 1.96，CI 寬約 10%）
  - 實作：`scipy.stats.t.ppf(0.975, n-1)`；無 scipy 時 hardcode 一張小表
  - 不用 HAC：per-city n 永遠停在 ~14（每天一筆），HAC 對 pooled 時間序列才
    有意義（DESIGN.md §4 用於全市每日誤差）
- **mean 與 median 並列**：n=14 時單筆異常（如 KHOU 4/23 lo=36.5°F）可把 mean
  拉開 ~3°F；median 較穩，兩者差距大時提示資料受異常影響
- 輸出按 |mean_err| 由大到小排序
- stdout 額外印 |mean_err| > 1°F 的列當警示

## 7. calibration_bins.csv

每個 `lock_cohort` 提供多筆 rows（cohort 內所有 bucket × side），每行：

- 解析 bucket 邊界（`lo-hi` / `>=N` / `<=N`）
- **先 `a = round(actual)`**（Polymarket 用 NWS 整數 reading 結算；不 round
  則 33.5°F 在 `32-33` 與 `34-35` 都 miss）
- YES side：
  - `lo-hi`：`hit = (bucket_lo ≤ a ≤ bucket_hi)`
  - `>=N`：`hit = (a ≥ N)`
  - `<=N`：`hit = (a ≤ N)`
- NO side：`hit = NOT YES_hit`

按 `our_prob` 切 0.05 寬度 bin（`[0,0.05), [0.05,0.10), ..., [0.95,1.00]`）：

```
bin_left, bin_right, n,
our_prob_mean, hit_rate,
ci95_low, ci95_high
```

### 7.1 CI：cluster bootstrap

> Wilson 假設 row 獨立，但同一天同 kind 的所有 bucket 共用 actual，YES/NO 為
> 互補；treated as independent 會讓 CI 太緊。

改用 cluster bootstrap：

- cluster 單位 = `(city, local_date, kind, market_slug)`（一個 lock_cohort
  為一群）
- 每次 resample 抽 N 個 cluster（with replacement，N = 原始 cluster 數）
- 整群的 rows 一起進入該次 resample
- 對每次 resample 重算每 bin 的 hit_rate
- B = 1000 次，取 2.5% / 97.5% 分位數做 CI95

實作上：DuckDB 算 base table，bootstrap 在 Python（pandas + numpy.random.choice
on cluster ids）。

## 8. 輸出位置

```
data/analysis/
  bias_per_city.csv
  calibration_bins.csv
  bias_per_city.txt        # human-readable summary
  calibration.txt          # ASCII calibration table
  diagnostics.txt          # lock_cohort 覆蓋率（三類）、IEM 異常 flag、
                          # CF6 vs IEM borderline 計數
```

腳本 idempotent，每次執行覆寫。

## 9. 待確認 / 風險清單（diagnostics.txt 對應項目）

### 9.1 lock_cohort 覆蓋率 — 拆三類成因

`no_lock` 定義：`(city, local_date, kind, market_slug)` 在 entry window
`[0.5, 12.0]h` 內**有 0 筆 EVAL row**。對失敗組合分類計數：

- (a) **市場全部 EVAL row 的 `hours_to_close < 0.5`**：entry window 完全錯過
  （市場過早關閉或 close 時刻未對齊）
- (b) **csv 最早記錄時間 > 該 market 的 entry window 結束點**：紀錄起始太晚，
  該市場的 entry window 已過
- (c) **其他**：predictor 中斷、市場未被 discovery 抓到、評估漏跑

僅 (c) 比例顯著（>5%）時才考慮放寬 entry window 或檢查 predictor 健康度。

### 9.2 IEM 異常值偵測（KHOU 4/23 lo=36.5°F 等）

保留但 flag，**不剔除**。規則：

- `|actual_high − actual_low| < 5°F`（日溫差過小）
- 相鄰日 `|actual_today − actual_neighbor| > 25°F`（突跳）
- 任一條成立 → diagnostics.txt 列出，bias 表加 `outlier_flag` 註解

### 9.3 樣本規模

11 城 × 13–14 日 × 2 kind ≈ 286 cluster；分 city 後每格 n=13–14，CI 寬，
結論視為訊號而非定論。

### 9.4 bucket parser

三種格式（`lo-hi` / `>=N` / `<=N`）已在 §3 確認；單元測試一個 parser
function 即可。

### 9.5 side 比例

calibration 表的 YES/NO 各 bin 比例若極不均，可選擇拆兩張 sub-table 看
（`calibration_bins_yes.csv` / `..._no.csv`）。

### 9.6 NWS CF6 vs IEM round-max 落差（borderline 統計）

> 原版用「距離 ≤1°F」當篩選——但 bucket 寬 2°F 且整數邊界，`round(actual)`
> 對最近邊界的距離恆 ≤1°F，100% 命中，無篩選力。改用 hit-flip 模擬。

對每個 lock_cohort：

1. 用 `a = round(actual)` 跑該 cohort 全部 bucket × side 的 hit/miss
2. 用 `a = round(actual) + 1` 與 `a = round(actual) − 1` 各重跑一次
3. 若任一 bucket × side 在三組中 hit 結果不一致 → 該 cohort 標 `borderline`

diagnostics.txt 印 borderline cohort 計數與比例。borderline 比例代表「CF6 vs
IEM ±1°F 抖動會翻轉多少校準訊號」，是真正的誤差上界估算。

## 10. 執行步驟

1. 寫 `audit/analyze_bias.py`（單檔約 250–300 行：DuckDB SQL + Python
   bootstrap + 輸出）
2. 執行：`python3 audit/analyze_bias.py`
3. 檢視 `diagnostics.txt` 的 lock_cohort 覆蓋率（三類）與 borderline 計數
4. 看 bias 與 calibration 結果，決定下一步（per-source 拆解、t_hat 時序圖、
   等更多樣本等）

## 11. 不在本次範圍

- HAC 標準誤、block bootstrap、BH FDR — 適用於 DESIGN.md §4 的 pooled 全市
  每日誤差時間序列（n 隨累積成長至 ≥90）；本計劃 per-city 的 n 停在 ~14，HAC
  無從派上用場，per-city CI 永遠用 Student-t
- per-source bias（nws / 5 OM models / ensemble）— 需要 audit_paired.csv 累積
- 自動更新 / 排程 — 本腳本是 ad-hoc 分析，不進 container

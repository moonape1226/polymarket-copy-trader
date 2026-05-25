# weather-audit 設計文件

版本：2026-05-01 修訂稿  
狀態：已部署（docker-compose service `weather-audit`）；本版新增資料完整性與統計門檻，需同步實作檢查

---

## 1. 背景

### 1.1 問題：交易樣本帶有選擇偏差

`weather_predictor` 的 paper-trader 從 2026-04-21 開始累積結算樣本（`data/paper_settled.csv`），
到 2026-04-28 為止 n=50 settled。觀察到 Miami 5 個 TIME_STOP 全敗、SF/Atlanta high 持續正偏差等現象後，
出現「是否要對某城市停止預測」的討論。

但這個問題不能用 `paper_settled.csv` 直接回答，因為：

1. **入場篩選**：predictor 只在 `edge ≥ threshold` 才下注（`predictor.py:947-976`）。
   一個系統性偏冷的模型，會在「實際比預測冷」的日子下 NO 倉，而錯過「實際比預測熱」的反向證據。
2. **TIME_STOP 截斷**：提早出場的記錄 `settle_temp_f` 為空，
   原本最有資訊量（極端 bias）的日子反而沒被結算。
3. **流動性/orderbook 過濾**：薄盤跳過、STUB_MULT 觸發跳過 ⇒ 結算樣本不是隨機抽樣。
4. **單側交易**：同一城市同一天可能只下 high 沒下 low，
   無法用單日的「兩側都下」這類自然對照消除偏差。

結論：**P&L 不是「預測準度」的無偏估計**。
要判斷模型對某城市是否系統性偏差，需要與交易脫鉤的觀測資料。

### 1.2 user 的指引

> 「跳過某個城市的預測是不好的做法 除非我們有證據顯示是來源資訊/模型有問題
> 僅靠短期資訊這樣判斷有失偏頗」

這直接對應 `project_weather_predictor_deferred.md` Priority 1（per-city bias correction）的
trigger condition：「per-city n ≥ 10 settled with `settle_temp_f` populated」。
但前述偏差讓這個條件用 `paper_settled.csv` 無法乾淨地滿足。

---

## 2. 目標與非目標

### 2.1 目標
- **G1**：每天每城市，**無條件嘗試 lock** 全部 6 個來源（NWS + 5 Open-Meteo）的當日 high/low 預測；coverage 不足的 source 明確記為空值。
- **G2**：在隔天用 station 觀測值配對；只有通過 coverage gate 的觀測才計算 `bias = actual − forecast`。
- **G3**：累積到可信統計樣本（per-city valid paired rows n ≥ 30）後，可獨立檢定：
   - 該城市是否有顯著系統性 bias（t 檢定 vs 0）
   - 哪個 source 對該城市最準（MAE/RMSE，而不是只看 mean bias）
   - bias 是否在 90-day rolling window 內漂移（季節性）
- **G4**：完全與交易脫鉤，零下單、零讀取 trading state。

### 2.2 非目標
- **NG1**：不影響 predictor 行為。本服務只寫 audit_*.csv/json，不讀也不改 paper_*.csv 或 predictor 配置。
- **NG2**：不嘗試與 Polymarket 市場結算 100% 對齊。Polymarket 用 Wunderground history（METAR :51 hourly），
  我們用 NWS observations endpoint。兩者來源同（METAR），路徑不同。差異本身是研究子題（見 §6.2）。
- **NG3**：不取代 predictor 的 ensemble 邏輯。本服務算的 `fcst_ensemble_*` 只是 reference baseline。

---

## 3. 資料來源

### 3.1 預測（locks 階段）

| Source | Endpoint | 解析度 | 與 predictor 對應 |
|---|---|---|---|
| NWS forecastHourly | `api.weather.gov/points/{lat,lon}` → `properties.forecastHourly` | 1h | `predictor.nws_fetch_forecast` |
| Open-Meteo ecmwf_ifs025 | `api.open-meteo.com/v1/forecast` | 1h | predictor 同 |
| Open-Meteo gfs_seamless | 同上 | 1h | predictor 同 |
| Open-Meteo icon_seamless | 同上 | 1h | predictor 同 |
| Open-Meteo gem_seamless | 同上 | 1h | predictor 同 |
| Open-Meteo ukmo_global_deterministic_10km | 同上 | 1h | predictor 同 |

集成方式：有效來源的算術平均（與 predictor 一致）。
缺值或 coverage 不足的 source 該日該欄為空且不進入 ensemble；不做依城市/來源權重、不補值，並記錄 `source_count`。

每個 source 需先通過 forecast coverage gate 才能進入欄位與 ensemble：

- 本地日範圍內至少覆蓋 `expected_hours - 2` 個小時（一般日 expected_hours=24，DST 日按 UTC duration 計）
- 第一筆預測時間不晚於本地 02:00
- 最後一筆預測時間不早於本地 22:00
- 任兩筆相鄰預測時間 gap 不超過 3 小時

### 3.2 觀測（finalize 階段）
- `api.weather.gov/stations/{station}/observations?start={iso}&end={iso}`
- 返回每筆 METAR 解碼後的 `properties.temperature.value`（°C）→ 轉 °F
- `actual_high = max(temps)`, `actual_low = min(temps)` over 本地日 0:00–24:00 區間
- 只有通過 observation coverage gate 才 finalize：至少 18 筆有效溫度、至少 20 個本地小時有觀測、最大觀測 gap ≤ 3h，且觀測橫跨本地日 02:00 前到 22:00 後
- station 對照（與 `predictor.CITIES` 同步）：

| City | Station | tz |
|---|---|---|
| Seattle | KSEA | America/Los_Angeles |
| NYC | KLGA | America/New_York |
| Chicago | KORD | America/Chicago |
| Dallas | KDAL | America/Chicago |
| Atlanta | KATL | America/New_York |
| Miami | KMIA | America/New_York |
| LA | KLAX | America/Los_Angeles |
| Houston | KHOU | America/Chicago |
| Denver | KBKF | America/Denver |
| Austin | KAUS | America/Chicago |
| SF | KSFO | America/Los_Angeles |

注意 Denver 使用 KBKF（Buckley AFB）而非 KDEN，與 Polymarket 結算和 predictor 一致。

---

## 4. 演算法

### 4.1 主迴圈
- Tick interval：30 分鐘（`AUDIT_TICK_SECONDS=1800`）
- 啟動時：載入 `audit_locks.json`（pending records），每 tick 對全部城市同步處理：lock 與 finalize。

### 4.2 Lock（每天每城市最多一次）
觸發條件：當地時間 0–6 時 且 `(city, today_local) ∉ locks`

流程：
1. `nws_hourly_periods(info)` → list of `{startTime, temperature_f}`
2. 對每個 Open-Meteo model 各取一份 hourly 序列
3. `filter_periods_by_local_day` 過濾出 `[day_start_local, day_end_local)` 範圍內的小時值，並檢查 forecast coverage gate
4. 該城市該日：
   - coverage 通過：`fcst_<source>_high = max(temps)`、`fcst_<source>_low = min(temps)`
   - coverage 未通過：`fcst_<source>_* = empty`
   - `fcst_ensemble_*` = 算術平均所有有效來源
5. 寫入 `audit_locks.json[f"{city}|{today}"] = record`

### 4.3 Finalize（觀測就緒後）
觸發條件：對 lock 紀錄 r，本地日結束時刻已過 ≥ 4h（等價於本地日開始後 ≥ 28h）。

流程：
1. `nws_observations(station, day_start_utc, day_end_utc)` → list of obs in 本地日 24h 範圍
2. 若回傳空或 observation coverage gate 未通過（API lag / station 缺資料）→ 保留 lock，下一個 tick 重試
3. 若超過本地日結束 7 天仍不完整 → append 到 `audit_rejected.csv`，原因標成 `obs_incomplete`，並從 locks 移除
4. 若所有 forecast source 都是空值 → append 到 `audit_rejected.csv`，原因標成 `no_valid_forecast`，不進入 paired 分析
5. 計算 actual_high/low、bias_*
6. Append 到 `audit_paired.csv`
7. 從 `audit_locks.json` 移除該 key

### 4.4 設計參數的選擇

| 參數 | 值 | 理由 |
|---|---|---|
| Lock window | 0–6 本地 | 涵蓋整個當日的逐小時預測最早可拉到的時段；接近 Polymarket 市場早盤交易時間，與 predictor 條件相近 |
| Tick | 30 min | 確保 Lock window 內至少觸發 8 次（容錯網路失敗）；也是 finalize 的重試節奏 |
| Finalize 延遲 | 本地日結束 +4h | NWS observations 偶有 lag（Chicago 留言區觀察過 > 1h 缺資料）；4h 緩衝足以避免大多 API lag，仍靠 coverage gate 防止部分資料污染 |
| Retry 上限 | 7 天 | 避免永遠 pending；不完整資料進 `audit_rejected.csv`，不進統計樣本 |
| Ensemble | 有效來源算術平均 | 與 predictor 一致；不做額外權重或補值，避免引入額外自由度 |
| Coverage gate | forecast: expected_hours-2；obs: ≥18 筆且 ≥20 小時 | 每日 high/low 對缺資料非常敏感，寧可少樣本也不要把部分日當完整日 |

---

## 5. 輸出 Schema

### 5.1 `data/audit_paired.csv`
每 (city, local_date) 一列，欄位定義：

| 欄位 | 型別 | 說明 |
|---|---|---|
| locked_at | ISO8601 UTC | lock 寫入時間 |
| city | string | CITIES key |
| station | string | airport code |
| local_date | YYYY-MM-DD | 該城市本地日 |
| tz | string | IANA tz name |
| fcst_nws_{high,low} | float \| empty | NWS 預測極值（°F）|
| fcst_<model>_{high,low} | float \| empty | 5 個 Open-Meteo 模型各自極值 |
| fcst_ensemble_{high,low} | float \| empty | 有效來源算術平均 |
| source_count | int | 進入 ensemble 的 source 數 |
| finalized_at | ISO8601 UTC | finalize 時間 |
| n_obs | int | 配對到的觀測筆數 |
| obs_hours_covered | int | 本地日中有至少一筆觀測的 hour 數 |
| obs_max_gap_hours | float | 相鄰觀測最大 gap |
| actual_{high,low} | float | station 觀測極值（°F）|
| bias_<source>_{high,low} | float \| empty | actual − fcst_<source>，source 包含 nws、5 個 Open-Meteo model、ensemble |

bias 正值代表「實際比預測熱」（預測偏冷）；負值反之。

### 5.2 `data/audit_locks.json`
`{f"{city}|{local_date}": record}` 的 dict。
record 是 finalize 前的 partial CSV row（無 actual_*、bias_*、finalized_at）。
finalize 成功後對應 key 被移除。

### 5.3 `data/audit_rejected.csv`
不進入統計樣本的 lock 記錄。主要欄位：`locked_at`, `city`, `station`, `local_date`, `tz`, `rejected_at`, `reason`, `n_obs`, `obs_hours_covered`, `obs_max_gap_hours`, `source_count`。

用途是 operational visibility，不可混進 bias/accuracy 分析。

---

## 6. 分析計畫

### 6.1 短期（n ≥ 30 / city，約 30 天後）

對每個 (city, source ∈ {nws, ensemble, ecmwf, gfs, icon, gem, ukmo}) × {high, low}：

- 計算 `mean(bias)` 與 raw `std / √n`（只作描述統計）
- 計算 accuracy：`MAE = mean(abs(bias))`、`RMSE = sqrt(mean(bias²))`
- t 檢定 `H0: mean = 0` 僅作 screening；正式判定使用 Newey-West/HAC stderr（lag=7）或 weekly block bootstrap，避免日資料自相關低估 CI
- 95% CI = mean ± 1.96 × HAC stderr
- 多重檢定：同一批城市/來源/side 使用 Benjamini-Hochberg FDR 控制（q ≤ 0.10）作為顯著性輔助，不單靠未修正 p-value
- **顯著偏差判定**：CI 不過 0 且 |mean| > 1.0°F

**輸出表**（每月手動執行 ad-hoc 分析腳本，不自動化）：

```
city    source     side  n    mean_bias   95%CI            MAE   RMSE  sig?
Miami   ensemble   high  30   +2.1        [+1.4, +2.8]     2.6   3.1   YES
Miami   ensemble   low   30   +3.5        [+2.6, +4.4]     3.7   4.2   YES
Miami   ecmwf      high  30   +0.8        [+0.1, +1.5]     2.2   2.9   borderline
...
```

### 6.2 中期（n ≥ 90 / city，約 90 天後）

- 90-day rolling bias：每日重算當前偏差，觀察是否漂移（季節性）
- 與 paper_settled.csv 對照：審計 bias 與交易結果方向是否一致
- 模型內部排序：哪個 Open-Meteo 模型對該城市最準？是否該降權某個 model？

### 6.3 觸發 predictor 修改

唯一允許 audit 結果觸發 predictor 修改的條件（嚴格）：

1. n ≥ 90 valid paired audit rows（非 `paper_settled.csv`；該 city/source/side 的 forecast 與 observation 都通過 coverage gate）
2. mean bias |Δ| ≥ 1.0°F
3. HAC 或 block-bootstrap 95% CI 完全在 0 同側
4. 通過同批多重檢定後仍顯著，或至少連續兩個 monthly batch 同方向顯著
5. 90-day rolling mean 與全期均值方向一致，且最新 30 天沒有明顯反轉
6. MAE/RMSE 顯示 offset 後預期會改善 accuracy，而不只是把 mean 拉回 0

滿足以上才考慮對該 city 在 predictor 加 bias offset。
即便如此，仍須與 user 確認再實作（feedback memory 規定）。

---

## 7. 邊界情境與限制

| 情境 | 處理 |
|---|---|
| NWS forecast endpoint 失敗 | 該城市該日 nws_high/low 為空，不阻塞 Open-Meteo |
| 某 Open-Meteo model 缺資料或 coverage 不足 | 該欄為空，不進入 ensemble |
| 全部來源都失敗 | lock 可暫存，但 finalize 時寫入 `audit_rejected.csv`，不進 paired 樣本 |
| NWS observations 24h 後仍未到齊 | finalize 失敗，lock 保留，下一 tick 再試；超過 7 天仍不完整則 reject |
| 容器重啟 | locks.json 持久化在 `/data`，pending records 不會掉 |
| 跨日邊界 lock 多次 | key = `f"{city}|{today_local}"`，per-day 最多一次 |
| Polymarket 改 settlement station | CITIES dict 與 predictor 同步更新；歷史資料以舊 station 標記，不回填 |

### 7.1 已知不嘗試解決的問題

- **Wunderground vs NWS observation 差異**：Polymarket 結算用 WU history（METAR :51）；
  我們用 NWS。兩者源頭都是 KMIA METAR，但 WU 有 post-processing（Seoul 留言區報告 64°F → 66°F）。
  本服務不主動拉 WU 對照（沒有公開 API；要做需要爬 history page）。
  若 audit 顯示 NWS bias 一致但 paper_settled 不一致，這個差異就是嫌疑。

- **METAR 非整點觀測（:15、:35）**：deferred Priority 4 的問題。
  本服務照 NWS 全收，不過濾 :51 only。若要對齊 WU，需後處理。

- **forecast lock 時間漂移**：早上 0:00 vs 6:00 lock 的預測不同。
  本服務不額外做 lock-time 對照（單一 lock per day），未來若需可加 hourly snapshot。

---

## 8. 部署

```bash
docker compose build weather-audit
docker compose up -d weather-audit
docker compose logs -f weather-audit
```

- 容器：`polymarket-weather-audit`
- 入口：`audit/audit.py`
- 依賴：`requests`, `tzdata`
- Mount：`./data:/data`（與其他服務共用 volume）
- 環境：`DATA_DIR=/data`、`AUDIT_TICK_SECONDS=1800`
- 重啟：`unless-stopped`
- 日誌：json-file，10MB × 3 rotation

健康檢查：log 中應每 30 min 出現一次 tick；每天每城市應出現一次 `lock` log；
每個 lock 應在隔日後進入 `paired` 或 `rejected`，若持續 pending 超過 7 天需檢查 API/coverage gate。

---

## 9. 開放問題

1. **lock window 是否應移到 Polymarket 市場開盤時點**？
   目前 0–6 本地是「當天最早能拉到完整預測」，但 Polymarket 市場通常前一晚開。
   若想對齊「下注時點的預測」，可改成前一晚 18:00 lock 預測 + 當天再 lock 一次。
   暫不做，等 30 天樣本評估再決定。

2. **是否需要 hourly forecast snapshots**？
   能回答「預測在當日內如何收斂」。但儲存量大（24×11×6×365 = ~580K rows/year）。
   暫不做。

3. **是否該對齊 WU history**？
   未實作公開 API。可考慮爬 history page 或用第三方包裝。
   現階段 NWS 已能驗證「forecast 對 NWS 觀測的偏差」這個第一階問題；
   「NWS 觀測 vs WU history」是第二階，先擱置。

4. **international markets**（London/Tokyo/Seoul/Toronto）：
   需要 °C unit 處理 + 不同 obs API（NWS 只覆蓋 US）。
   deferred；本服務目前只跑 US。

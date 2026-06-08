# Intraday Observation-Speed Edge — Live Test Spec

> **STATUS: CLOSED — KILLED BY STEP 0.1 (2026-06-08). Do not build the live monitor.**
>
> Step 0.1 ran and failed the gate. Empirical CF6 (Polymarket's settlement source)
> vs the best live-accessible observation (1-minute ASOS, spike-filtered, n=19 clean
> station-days):
> - **HIGH: 37% bucket-flip**, mean +0.58°F (95% within 1°F, but 2°F buckets flip anyway)
> - **LOW: 32% bucket-flip**, mean +0.16°F
>
> The edge's core premise — "after the afternoon peak the outcome is KNOWN" — is false.
> Even with perfect speed, our observed extreme picks a *different* 2°F settlement bucket
> than the official CF6 ~1/3 of the time. A bucket you believe is YES-certain at 0.90 is
> really only ~65% likely to settle YES → buying it is hugely −EV. This is a settlement-
> source noise floor, independent of (and additional to) codex's executability objection
> (retail REST can't beat MM sub-second CLOB-WebSocket repricing). Pincer: the only days
> the obs lands deep-inside a bucket (flip-safe) are exactly the days the market reads it
> just as easily and has already converged → no lag to capture. Both edges fail together.
>
> **Conclusion: the intraday edge — the last surviving weather-direction hypothesis — is
> dead. The entire weather-prediction-market direction has no retail-capturable edge.**
> Test scripts: the Step 0.1 comparison (CF6 via IEM `json/cf6.py?year=2026`, 1-min ASOS
> via `request/asos1min.py`) is reproducible from this session's transcript.

---


## Thesis

The daily temperature high is physically determined by ~2pm local (median; 100% of
25 sampled city-days locked by 5pm), but Polymarket weather markets resolve at
midnight. That leaves a ~10-hour window where the outcome is **already observed**
but may still be **forecast-priced**. If the market is slow to reprice after the
peak passes, we can trade buckets whose outcome is already near-certain.

This is NOT a forecasting edge (that was tested and is dead — market Brier 0.140
beats our best free mixture 0.150). It is a **speed/latency edge**: live METAR vs
a lagging order book.

**Honest prior (per codex review): the most likely outcome is NO exploitable edge.**
Professional MMs almost certainly subscribe to METAR push feeds and reprice within
seconds via CLOB WebSocket; a retail REST poller (200–500ms order round-trip) cannot
win that race. This test is therefore framed primarily as a **falsification**: prove
the lag is too small/too thin to capture, and close the weather direction for good.
A positive result is the surprise case, and must clear a high bar (below) to be
believed. Historical book_snapshots can't answer it (watchset too sparse) — but the
CLOB `/trades` endpoint and a short live run can.

## What we are measuring (test, not trade)

For each tracked US city-day, log a time series of:
- `obs_max_so_far` / `obs_min_so_far` from live METAR (the running extreme)
- `locked` flag: peak has passed (temp now falling N°F below obs_max for the high;
  rising N°F above obs_min for the low) → final extreme essentially known
- For each near-the-money bucket: live best_bid / best_ask / mid from CLOB
- `implied_outcome`: given obs-so-far + remaining-hours physics, is this bucket
  already YES-certain, NO-certain, or still live?

The edge exists if, in the post-lock window, the **now-certain bucket still trades
< 0.90** (buyable cheap) or a **now-impossible bucket still trades > 0.10**
(sellable rich), for long enough to execute.

## Data sources (all free, verified accessible 2026-06-08)

- **METAR**: `https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=2`
  — hourly + SPECI, ~minutes latency. Stations from `weather_predictor/predictor.py` CITIES.
- **CLOB book**: `https://clob.polymarket.com/book?token_id={tid}` — live bid/ask + depth.
- **CLOB trades**: `https://clob.polymarket.com/trades?market={condition_id}` — actual
  executed fills; the only proof a capturable lag existed (resting quotes don't count).
- **Event/bucket discovery**: `https://gamma-api.polymarket.com/events`
  (active, closed=false, order=endDate ascending, limit=100 — note the 100 server
  cap and 10100 offset cap; same pagination bug already fixed in predictor and
  book-logger).

## Lock detection

Two **separate** thresholds (codex: do not conflate trigger with safety margin):
- `LOCK_TRIGGER` = 4°F — peak considered passed when `current_temp <= obs_max - 4`.
- `RESPIKE_GUARD` = 2°F — additionally require the last 90 min of obs to be
  monotone-non-increasing within this band; a re-warm > RESPIKE_GUARD re-opens the lock.

Per city-day, kind=highest:
- `obs_max = max(temp observed so far today, local tz)`
- locked when `current_temp <= obs_max - LOCK_TRIGGER` AND `local_hour >= 15`
  (DST-aware: derive local_hour from the city tz, not a fixed UTC offset — the
  spring/fall DST boundary day shifts it 1h) AND the RESPIKE_GUARD monotone check holds.
- A bucket `[lo,hi)` is YES-certain if `lo <= round(obs_max) < hi`, NO-certain otherwise.
  Open tails `>=N` certain-YES if `obs_max >= N`.

**Coastal / marine-layer cities (SF, LA, Seattle): stricter.** Afternoon cloud
burn-off or sea-breeze reversal can re-warm 2–4°F after 3pm, defeating a 4°F trigger.
For these, require `local_hour >= 17` AND LOCK_TRIGGER = 5°F, or exclude them from the
first run entirely and add back only if inland cities show signal.

Mirror for kind=lowest (obs_min, temp rising past obs_min + LOCK_TRIGGER, near dawn).

## Output

`data/analysis/intraday_probe.csv`, one row per (city, kind, local_date, bucket, snapshot_ts):
```
ts, city, kind, local_date, bucket, station, local_hour,
obs_max_so_far, obs_min_so_far, current_temp, locked,
bucket_status (yes_certain|no_certain|live),
best_bid, best_ask, mid, spread,
mispriced_edge   # if locked: (1 - best_ask) for a yes_certain bucket priced <0.90,
                 # or best_bid for a no_certain bucket priced >0.10; else blank
```

## Step 0 — cheap pre-test before any live run (codex)

Before the 5–10 day run, two zero-/low-cost checks that can kill the idea outright:

1. **Quantify CF6-vs-METAR systematic bias.** Join `data/nws_actuals.csv` (IEM ASOS
   daily extremes ≈ METAR-derived) against the actual Polymarket settlement (the
   `won`/`settle_temp_f` in `data/paper_settled.csv`, which reflects CF6). Measure how
   often `round(obs_max)` would have called a bucket differently than the real
   settlement. If that disagreement rate is high near boundaries, the edge is dead on
   arrival (see "Settlement-source mismatch" risk below).
2. **48h trades-based probe instead of a full live run.** Poll CLOB `/trades` for
   tracked buckets; after a city's lock time, check whether any *actual fills* occurred
   at a price the post-lock outcome already contradicted. Resting quotes don't count —
   only executed trades prove a capturable lag existed.

## Success criterion (go / no-go) — quantified

Run 5–10 days, 11 US cities, only after Step 0 doesn't kill it. Edge confirmed only if
ALL hold:
- **≥ 30** distinct (city, local_date, bucket) post-lock observations with
  `mispriced_edge > 0.05` (raises the bar above multiple-comparison noise across
  11 cities × 10 days; a handful of rows is not a result).
- Mispricing **persists ≥ 3 consecutive snapshots** (≥ ~4–5 min) — rules out 1-tick
  ghost/stub quotes.
- The mispriced quote has **executable depth ≥ $50** at that level (not a $1 stub).
  Below this, "mispricing" is just a liquidity vacuum nobody will fill against.
- The bucket is **deep-certain**, not borderline: `obs_max` is **≥ 1.5°F inside** the
  bucket (so a ±1°F CF6/METAR discrepancy can't flip it).
- After subtracting the half-spread you'd actually pay, **net edge stays > 0.03**.
  (Note: post-lock spreads are typically very tight, 0.02–0.05, so net room is small
  even if a gross gap exists.)

If near-zero — the realistic expected case — the market reprices within seconds of the
peak and the intraday edge is arbitraged away → **close the weather direction entirely.**

## Non-goals

- No order placement. Pure measurement (read-only METAR + CLOB).
- Do not reuse `weather_predictor` — it's a forecasting bot; this is an
  observation-monitor. New small script `audit/intraday_probe.py`.
- No Kelly / sizing logic until the edge is confirmed real.

## Risks / caveats

- **Settlement-source mismatch is the most dangerous structural risk (codex).** The
  edge naturally lives at bucket boundaries — exactly where METAR vs CF6 (the NWS daily
  climate report Polymarket settles on) can disagree ±1°F. Excluding borderline buckets
  (the deep-certain ≥1.5°F-inside rule above) is not just conservative — it removes most
  theoretically-tradeable cases. Step 0.1 quantifies whether anything survives. If the
  CF6/METAR disagreement is large, the edge is dead regardless of market lag.
- **Executability ≠ visibility (codex).** Even persistent post-lock mispricing is most
  likely a *liquidity vacuum* (no counterparty) rather than slow repricing. A retail
  REST poller (200–500ms) loses to MM CLOB-WebSocket repricing (ms). The `/trades`
  check (Step 0.2) is the honest test: did a laggy fill actually happen, or is it just
  an un-hittable quote? Resting quotes prove nothing.
- Late re-spike / multi-peak / frontal passage can break an early lock — LOCK_TRIGGER
  vs RESPIKE_GUARD separation + monotone check + local_hour gate mitigate; cap exposure
  on marginal buckets and never act inside the RESPIKE_GUARD band.
- Polymarket $1 min order size vs a 0.02–0.05 post-lock spread leaves very little net
  room; size and fee/spread must be modeled before any "edge" is believed.

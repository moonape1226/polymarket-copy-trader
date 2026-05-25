# Scanner Optimize Spec — 2026-05-19

Derived from the 4/22→5/19 calibration dataset (`data/scan_history.json`, 169 resolved).

## Evidence

| Slice | Hit | EV |
|---|---|---|
| ALL | 161/169 (95.3%) | +5.9% |
| ex-crypto | 115/117 (98.3%) | +9.3% |
| ex-crypto ex-iran (forward proxy) | 65/67 (97.0%) | +7.8% |
| crypto only | 46/52 (88.5%) | **−1.7%** |
| 80-85% band | 44/49 (89.8%) | implied 82.5% → ~7% edge, stable over 3 weeks |

Every crypto event in the dataset is a **price-strike market** (`Bitcoin above ___ on DATE?`,
`What price will <coin> hit in April?`). No non-price crypto was actually tracked
(DeepSeek/model-ranking events classify as `ai`, not crypto). Crypto is the single
confirmed net-negative slice; removing it lifts overall EV +5.9% → +9.3%.

UMA dispute exposure is low except titles whose embedded date contradicts `endDate`
(`Bab el-Mandeb … by May 31?` with endDate 4/30 — stuck 463h / 19 days).

## Change 1 — Block crypto price-strike markets (HIGH confidence)

**Goal:** drop coin-price-strike events; do NOT block crypto-adjacent non-price
markets (ETF approval, delisting, protocol release) which may carry edge.

**Approach:** mirror existing `SPORTS_TITLE_PATTERNS` infra. Add a regex list
`CRYPTO_PRICE_PATTERNS` and a `looks_like_crypto_price(title)` helper, applied in
`scan()` at the same place the sports/esports skip happens (event-title level).

Match = a coin token **and** a price/direction token:

```
COIN  = bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|cardano|ada|bnb|avalanche|avax|litecoin|ltc
PRICE = above|below|dip|reach|hit|\$\s?\d|price will|be above|be below
```

A title is a crypto-price market iff it contains a COIN token AND a PRICE token.
Examples that MUST match (block): `Bitcoin above ___ on May 7?`,
`What price will Solana hit in April?`, `Will Ethereum reach $2,600 in April?`.
Examples that MUST NOT match (keep): `DeepSeek V4 released by April 30?`,
`Which company has the best AI model end of April?`,
`Will the SEC approve a Solana ETF by June?` (has coin but the
intent is regulatory; acceptable to over-block here — flag for codex opinion).

Remove the now-redundant note in the module docstring about crypto being
re-enabled; update CLAUDE.md scanner paragraph (`Excludes crypto-price, sports…`
already says this — verify still accurate after change).

### Change 1 — STATUS: IMPLEMENTED 2026-05-19 (post codex review)

Codex review applied: require COIN **and** PRICE token co-present (resolves the
ETF over-block contradiction — non-price crypto like "Solana ETF" is kept);
`\b` word boundaries on all coin tokens; helper checks both event title
(event-level skip) and `market["question"]` (per-market guard for generic
titles). Calibration integrity preserved — `check_resolutions()` iterates
history independently of `scan()`, so already-tracked crypto still resolves and
ages out via 30-day retention; only new crypto-price entries are excluded.
Acceptance replay: 27/27 crypto-price events blocked, 0 non-crypto false
positives, keep-list (DeepSeek/AI-model/Solana-ETF/Coinbase-delist) all kept.

## Change 2 — Title/endDate date-consistency skip (DEFERRED per codex)

**Decision: deferred.** The one stuck market's date (`May 31`) lived in the
`question` field, not the event title — title-only parsing would miss the very
case it targets. Free-text date parsing carries high false-positive surface
(month-as-verb, "before summer", Q-notation) for a ~1-market/4-week benefit.
Revisit only if UMA date-mismatch disputes recur across multiple cycles.

### Original Change 2 proposal (not implemented)

**Goal:** skip markets whose title embeds a calendar date materially later than
`endDate`, the pattern behind the only long-stuck UMA dispute.

**Approach:** parse the last `Month DD` / `Month DDth` / `by DATE` token from the
title; if a date is found and it is more than 2 days after the parsed `endDate`,
skip the market. Conservative: only skip on a *confident* parse; on any parse
ambiguity, keep the market (fail-open).

**Risk:** free-text date parsing → false positives. Only 1 market in ~4 weeks was
affected. Codex to assess whether the benefit justifies the parsing surface, or
whether a narrower rule (skip only if title month-name ≠ endDate month-name AND
title day > endDate day) is safer.

## Non-changes (explicitly out of scope)

- 80-85% band: no code change. Keep observing; it is the core edge signal.
- No change to `MAX_MARKETS_PER_EVENT`, `MIN_PROB/MAX_PROB`, intervals, liquidity floors.
- No trading/execution code. This spec is scanner-filter-only.

## Acceptance

1. `python3 -c "import ast; ast.parse(open('scanner/scanner.py').read())"` passes.
2. Replay current `scan_history.json` event titles through the new helpers:
   - all 27 crypto-price events → blocked
   - `Bab el-Mandeb … by May 31?` → blocked by Change 2
   - zero non-crypto, non-sports events in the existing dataset get newly blocked
     (regression guard — print the diff list).
3. Scanner rebuilds and starts clean; next scan logs no crypto-price events.
4. Existing `scan_history.json` is NOT wiped — new rules apply to new entries only.

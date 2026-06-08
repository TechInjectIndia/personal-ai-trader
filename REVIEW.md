# REVIEW.md — live-watch checklist for shipped-but-unproven features

**Purpose.** Some features are shipped on a hypothesis and must be judged on *live*
behaviour, not unit tests. This file is the standing watch-list. Each shipped
feature I'm skeptical of gets a row here + persistent instrumentation, and is
reviewed on a cadence until it graduates (proven good), is tuned, or is reverted.

## ⭐ Session-start ritual (do this FIRST, every session in this repo)
1. **Read this file** (REVIEW.md) — the watch-list + each feature's success criteria.
2. **Fetch the instrumentation:** `python scripts/review_digest.py`
   (reads Postgres signals — `decisions`/`audit`/`agent_runs`/`paper_trades` —
   and `logs/instrumentation/*.jsonl`).
3. **Report to the user:** per watch-list feature, how it performed vs its success
   criteria, and the recommended next action (graduate / tune / revert / keep watching).
4. **Update this file:** append a dated line to each feature's _Observations_ log;
   move graduated/killed features to the bottom section.

Instrumentation helper: `helm/instrument.py` `log_event(feature, event, **fields)`
→ append-only `logs/instrumentation/<feature>.jsonl`. Most features also emit
durable signals to Postgres already (audit/decisions/etc.) — the digest reads both.

---

## Watch-list

| Feature | Shipped | Hypothesis | Success criteria | Cadence | Status |
|---|---|---|---|---|---|
| `bbands_zscore_20` strategy | 2026-06-08 | mean-reversion has positive edge where ORB fails | gross expectancy > 0 **and** E2C ≥ 3 over ≥20 trades; beats ORB | every session + weekly | 🟡 watching (0 trades yet) |
| #681 stop lock-in (breakeven + EOD tighten) | 2026-06-08 | locks give-back without clipping winners | house EOD/STOP net ↑ vs baseline; not stopping out trending winners early | every session + weekly | 🟡 watching |
| F2 min-edge gate (`MIN_EDGE_TO_COST=3`) | 2026-06-08 | blocks sub-cost scalps, lifts net expectancy | blocks the right trades **without** starving the book (trades/day not ~0) | every session | 🟡 watching |
| F4 bigger-move reframe (`MIN_TARGET_PCT=0.6%`) | 2026-06-08 | bigger targets raise E2C & restore payoff (momentum only) | realised E2C ≥ 3 on house book; net expectancy ↑ | every session + weekly | 🟡 watching |
| F5 confidence↔outcome | measure 2026-06-08 | decider confidence predicts win | monotone: higher conf bucket → higher win%/net. **Gates** building conviction sizing | every session | 🟡 measuring (early +) |
| Daily post-close cadence | 2026-06-08 | daily loop iterates safely overnight | ≥1 pm/eng/tester run per trading day; no overnight regression survives to open | every session | 🟡 watching |

Legend: 🟡 watching · ⏳ landing · 🟢 graduated (proven) · 🔴 reverted/killed · 🔧 tuned.

---

## Per-feature detail

### `bbands_zscore_20` (A/B vs ORB)
- **How to check:** digest "Per-strategy economics" row for `bbands_zscore_20`; compare gross/net/E2C/win% vs `orb_15m`/`orb_5m`.
- **Decide:** graduate if gross expectancy>0 & E2C≥3 over ≥20 trades; tune params (N, K, MIN_TARGET) if close; drop from ACTIVE if clearly negative-gross after ~40 trades.
- **Observations:** _(none yet — 0 trades)_

### #681 stop lock-in
- **How to check:** digest "#681 stop lock-in" block — `stop_tightened` event count + house exit-reason mix. Watch for STOP exits that book gains (good) vs early stop-outs of would-be winners (bad).
- **Decide:** tune `EXIT_BREAKEVEN_TRIGGER_R` / `EXIT_TIME_DECAY_*` in config.py if it clips trending winners; keep if EOD give-back shrinks.
- **Observations:** _(none yet)_

### F2 min-edge gate
- **How to check:** digest "F2 min-edge gate" block (blocks today/7d/all) + trades/day trend. Cross-check: is net expectancy improving while the book isn't starved?
- **Decide:** if trades/day collapses, lower `MIN_EDGE_TO_COST` (or revert with `=0`); if bleed continues with healthy volume, hold.
- **Observations:** _(none yet)_

### F4 bigger-move reframe
- **How to check:** digest "F4 bigger-move" block — avg target distance % per momentum strategy should sit ≥0.6%. Then watch E2C on the per-strategy block.
- **Design note:** floor applies to MOMENTUM only (ORB/VWAP/gap_fade widen up to the floor). **Mean-reversion is EXEMPT** (`MEANREV_WIDEN_OR_DROP="none"`) — its target IS the mean, so a floor would near-disable bbands; its cost discipline is the F2 E2C gate. `"drop"`/`"widen"` are opt-in experiments. Also lowered `max_signals_per_symbol_per_day` 5→2. (`per_symbol_cooldown_min` 30→45 is currently inert — not wired in risk.evaluate.)
- **Decide:** tune `MIN_TARGET_PCT`; if the book starves, raise the signal cap back.
- **Observations:**
  - 2026-06-08 — shipped. Reversion exempted after adversarial review found the 0.6% floor near-disabled bbands under real large-cap σ (gutting the A/B) and regressed 2 tests; fixed + re-tested both modes.

### F5 confidence↔outcome (measurement only — sizing NOT built)
- **How to check:** digest "F5 gate" block — win%/net by confidence bucket. We need a **monotone** relationship before building conviction sizing.
- **Decide:** if higher conf → better outcome holds over ≥50 TAKEs, build F5 conviction sizing; if flat/noisy, do NOT build it (confidence is uncalibrated).
- **Observations:**
  - 2026-06-08 — measurement shipped (read-only; sizing NOT built). First reading: n=24 conf-tagged closed TAKEs, Pearson **+0.28**, win% climbs 17%→36%→**75%** across bands. Promising monotonicity but far below the ≥50-trade bar — keep watching, do NOT build sizing yet.

### Daily post-close cadence
- **How to check:** digest "cadence" block — pm/engineer/tester runs per day; cross-ref releases verified vs reverted.
- **Decide:** if overnight regressions reach the next open, tighten the Tester gate; if LLM cost too high, trim retro ticks.
- **Observations:** _(none yet)_

---

## Graduated / retired
_(empty — nothing has graduated or been killed yet)_

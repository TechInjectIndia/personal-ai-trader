# REVIEW.md — live-watch checklist for shipped-but-unproven features

**Purpose.** Some features are shipped on a hypothesis and must be judged on *live*
behaviour, not unit tests. This file is the standing watch-list. Each shipped
feature I'm skeptical of gets a row here + persistent instrumentation, and is
reviewed on a cadence until it graduates (proven good), is tuned, or is reverted.

> **📍 BASELINE RESET 2026-06-08 (20:45 IST):** all 6 books wiped to a fresh ₹50,000 (`scripts/reset_books.py`; archives `*_reset_20260608_204457`). Every metric below now measures from this clean slate under the full F1–F8 + cost-gate regime + risk-keying ON. The F5 ≥50-trade calibration counter restarts from 0. Pre-reset history is in the archive tables.

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
| F6 multi-timeframe (`_5m` variants) | 2026-06-08 | 5-min bars capture bigger moves vs the same fixed cost | `_5m` variants show higher E2C / better net than their 1-min twin | every session + weekly | 🟡 watching (0 trades yet); **A/B unblocked — risk-keying ON 2026-06-08** |
| F7 Context Engine (microservice) | 2026-06-08 | external news/event context lifts decision quality where the chart can't | claude-full beats claude-blind: positive gross expectancy + E2C≥3 on context-fed decisions | weekly (after shadow) | 🟡 built, OFF by default (shadow) |
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
- **Design note:** floor applies to MOMENTUM only (ORB/VWAP/gap_fade widen up to the floor). **Mean-reversion is EXEMPT** (`MEANREV_WIDEN_OR_DROP="none"`) — its target IS the mean, so a floor would near-disable bbands; its cost discipline is the F2 E2C gate. `"drop"`/`"widen"` are opt-in experiments. Throttle: `max_signals_per_symbol_per_day` 5→2→**3** (rebalanced), and `per_symbol_cooldown_min`=45 is **NOW ENFORCED** in `risk.evaluate` (was inert; wired 2026-06-08 via `minutes_since_last_exit`).
- **Decide:** tune `MIN_TARGET_PCT`; if the book starves, raise the signal cap back.
- **Observations:**
  - 2026-06-08 — shipped. Reversion exempted after adversarial review found the 0.6% floor near-disabled bbands under real large-cap σ (gutting the A/B) and regressed 2 tests; fixed + re-tested both modes.

### F5 confidence↔outcome (measurement only — sizing NOT built)
- **How to check:** digest "F5 gate" block — win%/net by confidence bucket. We need a **monotone** relationship before building conviction sizing.
- **Decide:** if higher conf → better outcome holds over ≥50 TAKEs, build F5 conviction sizing; if flat/noisy, do NOT build it (confidence is uncalibrated).
- **Observations:**
  - 2026-06-08 — measurement shipped (read-only; sizing NOT built). First reading: n=24 conf-tagged closed TAKEs, Pearson **+0.28**, win% climbs 17%→36%→**75%** across bands. Promising monotonicity but far below the ≥50-trade bar — keep watching, do NOT build sizing yet.

### F6 multi-timeframe (`_5m` variants)
- **How to check:** digest "Per-strategy economics" — compare `bbands_zscore_20_5m` / `vwap_reclaim_5m` / `gap_fade_5m` vs their 1-min twins (E2C, net, win%). ACTIVE now has 8 strategies (5 × 1-min + 3 × 5-min). ORB is intentionally NOT resampled (orb_5m/orb_15m are opening-RANGE minutes, not 5-min bars).
- **Known measurement caveats (logged, NOT safety bugs):**
  1. **A/B confound** — the risk gate keys on *symbol* only, so a `_5m` and `_1m` variant on the same symbol contend for one position slot (whichever fires first blocks the other). Cleanly fixing needs `(symbol, strategy)` keying — a deliberate position-concurrency change across all strategies, deferred. Until then read the per-timeframe comparison as *directional*, not clean.
  2. Decider builds its LLM context from 1-min bars even for a `_5m` signal (`decide_signals.py` `todays_candles`); trade params are unaffected (they come from the Signal). Follow-up: feed `resample_candles(symbol, bar_minutes)` for parity.
  3. `bbands_zscore_20_5m` won't arm until ~11:05 IST (MIN_BARS=22 in *bars*); `gap_fade_5m` has a thin pre-09:45 window → both fire rarely (the "fewer-bars/slower-stats" tradeoff).
- **Decide:** if a `_5m` variant shows materially higher E2C/net than its 1-min twin over ≥20 trades, promote the timeframe; if it never fires meaningfully, drop it. Revert = remove the 3 entries from ACTIVE (one line).
- **Observations:**
  - 2026-06-08 — shipped (resample_candles + bar_minutes-aware scan + 3 variants). 0 trades yet.

### F7 Context Engine (decoupled microservice)
- **Architecture:** standalone FastAPI service in `context_engine/` (own process, own `context_items`/`context_scores` tables, free yfinance-news source, `helm.llm` scorer with time-decay). The bot consumes via `helm/context_client.py` (stdlib HTTP) **only when `CONTEXT_ENGINE_URL` is set** — unset = byte-identical to today (clean A/B baseline), and any error/timeout/stale → fail-open (no context, never blocks a trade). Bot never imports `context_engine/`.
- **Enable (A/B on):** start the service (`context_engine/run.sh`, needs `pip install -r context_engine/requirements.txt`), schedule `scripts/ingest_context.py` on cron, set `CONTEXT_ENGINE_URL=http://127.0.0.1:<port>` in `.env`. **Disable/revert:** unset the env var (instant).
- **How to check:** `logs/instrumentation/context_engine.jsonl` (hit/miss/fail_open events) + the per-strategy/decider economics once shadow data exists. Run ≥2 weeks in SHADOW (ingest+score, flag OFF) and validate score-vs-next-move before trusting it to trade.
- **Live-safety verified (2026-06-08):** flag-unset → None on first line (zero path); flag-set-but-service-dead → None in ~34ms (bounded, no raise); fail-open on non-200/timeout/garbage. Decider prompt is byte-identical when context is None.
- **Observations:**
  - 2026-06-08 — built (service + flag-gated fail-open consumer + ingest cron + tests). Shipped OFF. Service `/health` 200, `/context/{sym}` returns neutral/stale until ingestion runs.

### Daily post-close cadence
- **How to check:** digest "cadence" block — pm/engineer/tester runs per day; cross-ref releases verified vs reverted.
- **Decide:** if overnight regressions reach the next open, tighten the Tester gate; if LLM cost too high, trim retro ticks.
- **Observations:** _(none yet)_

---

## Built but flag-gated OFF (enable when evidence clears)
These mechanisms are shipped, tested, and inert until their flag flips. **Toggle them live from the dashboard → ⚙️ Settings → "Feature flags"** (writes the `settings` table; the bot picks it up next cron tick, no restart). Their on/off state + per-strategy economics are visible on the **Self-Improvement** page. Check the digest before flipping.

| Feature | Flag (dashboard Settings / `.env`) | Adversarial review (2026-06-08) | Enable when |
|---|---|---|---|
| F5 conviction sizing | `CONVICTION_SIZING_ENABLED` | 🔴 NO-GO — code correct, but it's a DATA gate | conf↔outcome holds over ≥50 closed TAKEs (digest "F5 gate"); still n≈24, +0.28 |
| F7-P3c context signals | `CONTEXT_SIGNALS_ENABLED` | n/a (needs engine first) | engine shadow-validated: context score predicts next move |
| F7 engine consumption | `CONTEXT_ENGINE_URL` (.env) | n/a | ready to start claude-blind vs claude-full A/B (after ≥2wk shadow) |
| Per-(symbol,strategy) slots | `HOUSE_STRATEGY_KEYED_SLOTS` | 🟢 ENABLED 2026-06-08 (per-symbol cap `MAX_OPEN_POSITIONS_PER_SYMBOL=2`) | ✅ ON — watch: each symbol ≤2 concurrent; 1m vs 5m variants now both trade (clean A/B). Revert: set flag False in Settings |
| F8 mandate advisory | _(live; advice-only)_ | — | competitors see their own E2C in the weekly mandate prompt |

_Review (ec406cd): the resolver was hardened to fail-safe (malformed settings → code default, never raises on the trade path) — a latent live bug, now fixed. F5 remains a data gate (do NOT flip yet). Risk-keying is now safe to enable (per-symbol cap caps correlated pile-up)._

## Graduated / retired
_(empty — nothing has graduated or been killed yet)_

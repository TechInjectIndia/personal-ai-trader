# Helm Self-Improvement Integration PRD

**Status:** Draft for sequencing — 2026-06-02
**Owner:** Helm (autonomous PM → Engineer → Tester loop), human as final gate
**Source of truth:** the 183 `improvement_proposals` rows with `status='accepted'`
after the 2026-06-02 semantic backlog drain (523 open → 15; the 15 remaining are
the inactive `zzz-freestyle-test` agent). Every item below cites the proposal
id(s) it folds in, so this PRD is fully traceable back to the DB.

---

## 1. Why this document exists

The retro pipeline generated ~550 proposals; the PM drain (Opus 4.8) deduped
them semantically into 183 distinct accepted ideas. But "183 accepted tasks" is
not a plan — many are restatements of the **same root fix seen from different
trades**, several are the same idea expressed at different layers (strategy vs
risk-gate vs decider-prompt), and they have hard **ordering dependencies** (you
cannot meaningfully enrich the decider payload with a reward-to-risk field while
the R:R is still computed from the wrong formula, and no filter is trustworthy
while the price feed still emits flat OHLC bars).

This PRD collapses the 183 into **8 phases in dependency order**. Each phase is
independently shippable and measurable in paper-mode before the next begins.

### Design principles (apply to every phase)

1. **Fix at the source, gate in depth.** The recurring themes (R:R floor,
   breakout clearance, stop width) appear in `strategy`, `risk`, AND
   `decider_prompt` proposals. Implement each as a **single parameter** in
   `helm/config.py`, enforced first at the layer that can suppress earliest
   (strategy), then re-checked at the deterministic risk gate, with the decider
   prompt as the final qualitative backstop. One source of truth, defense in
   depth.
2. **Deterministic before LLM.** Anything that can be a pure-Python gate
   (staleness, duplicate-symbol, capital, R:R floor) runs *before* the decider
   so we never spend an LLM call on a structurally-dead signal.
3. **Reversible + parameterised.** Every threshold is a named config constant
   with a conservative default and a comment on how to tune. No magic numbers
   inline.
4. **Measure in paper-mode.** Each phase ships behind the existing paper-trade
   path and is judged on the next retro cycle's `decision_quality_score` and
   realised P&L before the following phase starts.
5. **One prompt rewrite, not 44 edits.** The 44 `decider_prompt` proposals are
   integrated as a *single* restructured system prompt (Phase 5), not 44
   incremental appends that would contradict each other and thrash the prompt
   cache.

---

## 2. Phase map (the integration order)

| Phase | Theme | Depends on | Accepted-proposal count* |
|---|---|---|---|
| **0** | Data integrity foundation | — | 3 |
| **1** | Signal math correctness (strategy core) | 0 | ~13 |
| **2** | Signal quality filters (strategy) | 1 | ~40 |
| **3** | Deterministic pre-decider gates | 1 | ~12 |
| **4** | Signal payload enrichment (data) | 0, 1 | ~16 |
| **5** | Decider prompt hardening | 4 | ~50 |
| **6** | Execution realism & trade management | 1 | ~22 |
| **7** | Observability & meta | all | ~5 |
| **8** | Per-competitor freestyle config (parallel track) | — | ~50 |

\* Counts overlap because one idea recurs across categories; the PRD implements
each *once* at the right layer.

The spine is **0 → 1 → 2 → 3 → 4 → 5 → 6 → 7**. Phase 8 (freestyle agents'
persona/config tweaks) runs **in parallel** — those self-apply per agent via
each competitor's own PM→Engineer and don't touch the house code path.

---

## Phase 0 — Data integrity foundation

**Goal:** trust the price feed before building anything on top of it.

**Why first:** every R:R calculation, breakout-clearance filter, range-width
filter, and stop-distance check reads `high`, `low`, and intra-bar range. The
retros surfaced **flat OHLC bars (H == L, zero intra-bar range)** in the
1-minute feed (proposal 523, confidence 5 — the single highest-recurrence
cluster, 4 restatements). A flat bar silently corrupts range width → corrupts
R:R → makes every downstream filter fire on garbage. This is the foundation.

**Folds in:** `523` (house), `gemini:flag-zero-intrabar-movement`,
`opencode:flag-single-price-bar-illiquidity` (35).

**Spec:**
- At `roll_minute_candles()` ingestion, compute a per-session data-quality stat:
  fraction of bars where `high == low` for each liquid large-cap.
- If > 30% of a session's bars are flat, write a `DATA_QUALITY_WARNING` to
  `audit` and set a per-symbol `data_quality_ok=false` flag the scanners read.
- Investigate root cause: is `yfinance` `fast_info.last_price` giving us tick
  prints that collapse to close-only bars? Document findings in `WORKAROUNDS.md`.
- Expose a `single_price_bar` boolean on each candle so strategies/decider can
  see illiquidity at signal time (consumed in Phase 4).

**Acceptance:** a session with synthetic flat bars raises exactly one warning;
clean sessions raise none; the flag is queryable per symbol/day.

---

## Phase 1 — Signal math correctness (strategy core)

**Goal:** fix the structural reward-to-risk bug so emitted signals mean what
they claim. This *changes which signals exist*, so it must precede every filter,
gate, and prompt rule that reasons about R:R.

**The bug (most-recurring theme in the whole backlog):** strategies compute
`target = range_high + rr_multiplier × or_width`, but actual reward is measured
from the **fill/entry price**, not the range high. When the trigger bar closes
well above the range high, the true `(target − entry) / (entry − stop)` collapses
below the configured 1.5×. The retros caught this from a dozen separate trades.

**Folds in (target formula):** `181, 182, 278, 590, 71, 187, 217, 229, 661, 128,
145, 112`; `kiro:29, 30`; `opencode:57, 63, 65`; `gemini:20, 21, 23`;
`nemotron:34`.
**Folds in (R:R hard gate at source):** `181, 217, 229, 661, 128`.
**Folds in (minimum stop width):** `631`, `gemini:21, 23`, `nemotron:34`,
`opencode:48, 63`.
**Folds in (multiplier tuning):** `256` (backtest 1.0×/1.2×/1.5×).

**Spec:**
- Replace target math in `orb_5m`, `orb_15m`, `gap_fade`:
  `target = entry + RR_MULT × (entry − stop)` — guaranteeing the stated ratio at
  any trigger price.
- Add a strategy-layer **hard R:R gate**: compute
  `actual_rr = (target − entry) / (entry − stop)` with the real entry; if
  `< RR_MULT`, do **not** emit (suppress, don't forward to decider).
- Add **minimum stop width**: `MIN_STOP_PCT` (default 0.003 of entry); below it,
  suppress with `stop_too_tight`.
- New config constants: `RR_MULT` (1.5), `MIN_STOP_PCT` (0.003). Keep per-strategy
  override hooks.

**Acceptance:** no signal is emitted whose entry-based R:R < `RR_MULT`; targets
recomputed from entry-stop distance; unit test pins the formula on the worked
examples in proposals 181/182/278.

---

## Phase 2 — Signal quality filters (strategy)

**Goal:** stop emitting noise breakouts. ~40 proposals, three filter families,
each unified into **one parameterised filter** rather than N hardcoded variants.

**2a. Minimum breakout clearance above the range boundary**
The single largest cluster. Many phrasings ("0.10%", "0.15%", "0.20%", "10% of
range width", "0.5× or_width") of one idea: a marginal poke past the boundary is
not a breakout.
Folds in: `50, 89, 199, 406, 442, 630, 12, 88, 166, 207, 212, 252, 588, 591,
130, 73, 80, 109, 601, 404, 162`; `opencode:60, 62`.
Spec: `min_breakout = max(MIN_BREAKOUT_PCT × close, MIN_BREAKOUT_RANGE_FRAC ×
or_width)`; require `close ≥ or_high + min_breakout` (symmetric for sells).
Defaults: `MIN_BREAKOUT_PCT=0.0015`, `MIN_BREAKOUT_RANGE_FRAC=0.10`.

**2b. Minimum opening-range width**
A range too narrow produces mechanical R:R failures and noise.
Folds in: `248, 218, 266, 270, 169, 204, 639, 679, 105`; `opencode:56, 64`;
`gemini:` (range filters).
Spec: suppress if `or_width / or_mid < MIN_RANGE_WIDTH_PCT` (default 0.0035;
ETF-specific floor `MIN_RANGE_WIDTH_PCT_ETF=0.008` for the *BEES instruments).

**2c. Range-violation, re-test, and hold-confirmation**
Suppress setups where the level was already pierced/rejected, or require the
breakout to *hold*.
Folds in: `74, 209, 230, 276, 78, 82, 91, 179, 23, 157, 267, 104, 113, 576,
601`; `gemini:19, 24, 25, 26`; `opencode:58, 60, 61, 62`.
Spec:
- Suppress a BUY if any bar since open traded `low < or_low` (range floor
  pierced); symmetric for sells (`range_violated_before_breakout`).
- Suppress a re-test: price already exceeded `or_high` then fell back below.
- Require **2-bar hold**: trigger only after `HOLD_BARS` (default 2) consecutive
  closes beyond the boundary.

**2d. Time-of-day gates**
Folds in: `95, 15, 20, 17, 131, 139, 94, 21`; `gemini:28`.
Spec: `MAX_SIGNAL_AGE_MIN` (default 90) after OR close → suppress;
`gap_fade` cutoff `GAP_FADE_LATEST=13:30` (suppress or halve target after).

**Acceptance:** each filter has a unit test on its worked example; aggregate
signal volume drops and average emitted-signal R:R rises vs the prior session.

---

## Phase 3 — Deterministic pre-decider gates

**Goal:** cheap, fail-closed Python gates *before* the LLM call. Each writes a
distinct skip reason (consumed by Phase 7's taxonomy).

**3a. Staleness gate** — reject any signal older than `STALE_SIGNAL_MIN`
(default 15) before the decider; log as `EXPIRED`, not `SKIP`, so latency is
tracked separately. Folds in: `4, 16, 158, 19`.

**3b. Duplicate-symbol block** — reject a new signal whose symbol already has an
open position (optionally same-direction only). Folds in: `192, 221` (+ prompt
mirror `208`).

**3c. Per-symbol minimum-capital gate** — before scanning a symbol, skip it
silently if `wallet.available < last_close` (can't afford one share). Folds in:
`280`.

**3d. Charges-aware gate** — block trades where gross profit at target is less
than `MIN_PROFIT_CHARGES_MULT × estimated_charges`, or where planned reward <
planned risk. Folds in: `opencode:51, 52, 53, 54, 59`.

**Acceptance:** gates run before any `complete_json` call; each emits its own
`skip_reason`; no LLM spend on signals these reject.

---

## Phase 4 — Signal payload enrichment (data)

**Goal:** give the decider the computed context the Phase 5 prompt rules will
reference. **Must land before Phase 5** — the new prompt cites these fields.

**Folds in:** `262` (reward_to_risk), `86` (floor_breached_before_signal,
max_floor_breach_inr), `231` (range_violated struct), `175` (prior intrabar OR
breach), `61` (median_daily_range_10d), `81` (bars_above_range_high), `103`
(current_price at decision time), `83` (session_range_so_far,
target_as_pct_of_session_range), `219, 234` (nifty direction / 5m chg),
`264` (momentum_ratio), `627` (pre_signal_dip/recovery), `78` (OR-low breach
flag); `gemini:1` (elapsed-move duration), `opencode:36`.

**Spec:** extend the strategy `Signal.payload` (and the dict passed to
`decide_signal_inline`) with the above computed fields. All pure functions of
candles already in `candles_1m` + the live `current_price`. No new data sources
except `NIFTYBEES` (already pollable via `yfinance`).

**Acceptance:** every signal payload carries the full field set; a golden-payload
test pins the schema; fields are present in the `decisions` audit trail.

---

## Phase 5 — Decider prompt hardening

**Goal:** one coherent rewrite of the decider system prompt folding 50
`decider_prompt` proposals into a structured rubric. **One rewrite**, because 50
incremental appends contradict each other and break prompt caching.

**Structure the new prompt as ordered hard gates → then qualitative judgment:**

1. **Hard gates (immediate SKIP, no further analysis):**
   - R:R floor: if computed entry-based R:R < `RR_MULT`, SKIP unconditionally —
     no qualitative override, no paper-mode exception. Folds in: `54, 100, 176,
     238, 246, 162, 448`; `opencode:46`.
   - Stale signal: `signal_time` > `STALE_SIGNAL_MIN` old → SKIP `stale_signal`.
     Folds in: `6, 19`.
   - Duplicate symbol: `risk_state.already_open_in_symbol` → SKIP. Folds in:
     `208`.
   - Near-close: after 14:00 IST, SKIP MIS ideas with target > 0.5% away. Folds
     in: `21`.
2. **Limit-order fill semantics:** `proposed_entry` is the limit fill price — do
   **not** re-derive R:R from live market price; only flag genuine staleness when
   price has already moved *past* entry. Folds in: `154, 151`.
3. **Marginal-breakout skepticism:** if clearance < ~0.15% or the model's own
   reasoning calls the move "marginal/tiny/borderline" with confidence < 0.70 →
   SKIP, or require ≥2 independent confirming factors. Folds in: `407, 57, 404,
   443, 529, 92, 66`.
4. **Re-test / boundary-violation:** a level pierced-and-rejected earlier is a
   standalone SKIP reason. Folds in: `180, 277, 23`.
5. **Pace-to-target & tape weakness:** when < 60 min to square-off, compare
   required pace to observed pace; measure post-signal pullback against *stop
   distance*, not the post-trigger move. Folds in: `132, 168, 186, 279, 623`.
6. **Decider independence (anti-rubber-stamp):** the AI must produce reasoning
   independent of the strategy rationale — never restate the signal's own
   justification as its approval. Folds in: `gemini:5, 7`; `opencode:37, 41, 38,
   39, 44`.

**Spec:** rewrite the cached system prompt in `scripts/decide_signals.py`;
expect a one-time cache-miss on first run (documented in CLAUDE.md). Keep strict
JSON output.

**Acceptance:** prompt references only fields delivered in Phase 4; a fixture
set of historically-bad trades now returns SKIP with the correct reason; cache
hit recovers after first run.

---

## Phase 6 — Execution realism & trade management

**Goal:** make paper fills honest and manage open trades actively. Independent
of signal generation, but sequenced after Phases 1–2 so we measure execution on
*good* signals.

**6a. Realistic fills** — set paper `fill_price` to the **next bar's open**
after the TAKE, not the signal-bar close/trigger. Folds in: `105, 245, 138`;
`gemini:11` (audit cross-bar gaps).

**6b. Breakeven & trailing stop** — once unrealised gain ≥ 40% of (target −
entry), move stop to breakeven; trail thereafter. Folds in: `1, 51, 239, 495`;
`gemini:10`; `opencode:49`.

**6c. Failed-breakout / stall exits** — exit at market if price falls back below
the breakout level within `FAILED_BREAKOUT_MIN` (default 30) of entry, or hasn't
covered `MIN_PROGRESS_FRAC` of the target within `TIME_EXIT_MIN`. Folds in: `202,
55, 236, 244, 414, 50`; `opencode:50`.

**6d. Slippage / gap buffer** — add a stop-slippage buffer (0.5% of entry) to
the risk used for sizing on sharp-drop strategies and thin ETFs. Folds in: `577`;
`opencode:47`; `gemini:9`.

**6e. Size-down-to-cap instead of block** (cross-cutting `paper_execute`) — when
only the notional cap blocks a trade, scale the quantity down to the cap rather
than rejecting the whole signal. Also audit the sizing formula (a 3× notional
overshoot suggests a misconfigured risk fraction). Folds in: `gemini:12, 16, 17,
18, 15`; `opencode:55`; `nemotron:33`.

**Acceptance:** fills use next-bar open; breakeven/stall exits fire on fixtures;
notional-capped signals now size down instead of skipping (distinct audit
reason).

---

## Phase 7 — Observability & meta

**Goal:** instrument everything above so the next retro cycle can measure it.

**Folds in:** `281` (skip_reason taxonomy: `analysis | wallet_blocked |
risk_gate | time_gate | stale | charges`; exclude `wallet_blocked` from
`decision_quality_score`); `gemini:14` (tag mechanical-cap skips separately);
`19` + `523` (data-quality + pipeline-latency alerting).

**Spec:** add `skip_reason` to every `decisions` row; split the digest/retro
aggregation by reason; surface the taxonomy on the dashboard's quality page.

**Acceptance:** every SKIP carries a reason; quality score no longer polluted by
capital/cap blocks; dashboard shows the breakdown.

---

## Phase 8 — Per-competitor freestyle config (parallel track)

The freestyle agents (`gemini-momentum`, `opencode-range`, `nemotron-trend`,
`kiro-orb`) accepted ~50 proposals that are **persona/config tweaks scoped to
that agent** — minimum-stop-width for high-priced names, two-bar fade filters,
late-entry chase filters, charges-aware bounce gating, etc. These integrate
**independently** via each competitor's own PM→Engineer path and must **not** be
merged into the house code. They are listed here only for completeness and as
validation: they strongly echo the house themes (R:R floor, stop width, late
entry, size-to-cap), which is independent confirmation that the spine above
targets the real problems.

**Action:** let the freestyle Engineer apply these per-agent on the normal
Sunday cadence; no cross-agent merging.

---

## 3. Sequencing summary & rationale

```
Phase 0  Data integrity        ── nothing is trustworthy on a broken feed
   │
Phase 1  Signal math (R:R)     ── changes which signals exist
   │
Phase 2  Signal filters        ── operate on validly-emitted signals
   │
Phase 3  Deterministic gates   ── cheap fail-closed checks before any LLM call
   │
Phase 4  Payload enrichment    ── the fields the new prompt will reference
   │
Phase 5  Decider prompt        ── one coherent rewrite, cites Phase-4 fields
   │
Phase 6  Execution realism     ── honest fills + active management on good signals
   │
Phase 7  Observability         ── measure all of the above for the next retro
   
Phase 8  Freestyle configs     ══ parallel, per-agent, independent
```

**Cross-phase dedup contract:** the recurring "enforce R:R ≥ 1.5" idea appears in
strategy (Phase 1), risk-gate (Phase 3), and decider-prompt (Phase 5). It is
**one** `RR_MULT` constant enforced at three layers — defense in depth, single
source of truth. The same holds for breakout clearance and stop width. The
Engineer must not create three independent thresholds.

## 4. What this unblocks

Each phase is a small, testable batch the autonomous Engineer can ship on the
Sunday cadence and the Tester can verify — instead of 183 unordered tasks
contending for the one-change-in-flight gate. After Phase 7, the loop has clean
data, honest fills, and a reason-tagged decision trail, so the *next* generation
of retros measures real signal/execution quality rather than re-discovering the
flat-bar and R:R-formula bugs from every trade.

# FRD F5 — Conviction-Weighted Sizing

_Phase 2 · Owner: house engineer loop · Status: proposed · Depends on: decider confidence (exists), F1_

## 1. Problem
Position size is flat regardless of conviction. Marginal, low-conviction setups are exactly where the ₹13 cost wins — and we size them the same as high-conviction ones, so costs are spread evenly over a coin flip.

## 2. Goal & success metrics
Concentrate capital in high-conviction trades and **skip** low-conviction ones entirely.
- Driver: realised expectancy of high-conviction bucket > low; fewer total trades.
- Outcome: net expectancy/trade ≥ 0; conviction correlates with win% (provable in F1 by bucketing on stored confidence).

## 3. HLD
The decider (`scripts/decide_signals.py`) already returns `confidence ∈ [0,1]` (stored in `decisions.reasoning`/structured output). Use it two ways at the house chokepoint:
1. **Floor:** confidence < `CONVICTION_FLOOR` → SKIP (don't open at all).
2. **Scale:** notional = `dynamic_position_cap(...) × conviction_multiplier(confidence)` — more size as conviction rises, within existing caps.

```
decider verdict=TAKE, confidence c
   c < CONVICTION_FLOOR ─► SKIP (reason='low_conviction')
   else qty = floor( min(cap * mult(c), wallet.available) / entry )   # mult(c) in [MIN_MULT, 1.0]
```

## 4. LLD
**Config (`helm/config.py`):**
```python
CONVICTION_FLOOR = Decimal("0.55")        # skip TAKEs below this confidence
CONVICTION_SIZE_MIN_MULT = Decimal("0.5") # smallest size multiple (at the floor)
# linear ramp: mult(c) = MIN_MULT + (1-MIN_MULT) * (c - FLOOR)/(1 - FLOOR), clamped [MIN_MULT,1]
```
**Plumbing:** `decide_signals.decide_signal_inline` already has the parsed `confidence`; pass it into `execute_signal(..., conviction=confidence)`.
**`execute_signal` (`scripts/paper_execute.py`):**
- New optional `conviction: Decimal | None = None`.
- If `conviction is not None and conviction < CONVICTION_FLOOR`: SKIP (decisions row, consumed) — mirror F2's skip path.
- Else sizing: `cap = dynamic_position_cap(realised, base)`; `eff = cap * conviction_multiplier(conviction)`; `budget = min(eff, wallet.available)`; `qty = budget // entry`. When `conviction is None` (manual runs), behave exactly as today (`mult=1`).
- Order vs F2: apply conviction floor → size → F2 E2C gate → `risk.evaluate`.

**League:** house-only. Competitors size themselves; their loop gets conviction/expectancy feedback via F8.

## 5. Test plan
`tests/test_conviction_sizing.py`: confidence below floor → SKIP; multiplier monotonic in confidence and clamped; `conviction=None` reproduces current sizing exactly (regression guard); interaction with `dynamic_position_cap` correct.

## 6. Rollout / revert
Start `CONVICTION_FLOOR` low (0.5) and `MIN_MULT` high (0.7) to avoid starving the book; tune via F1 once confidence↔win% is measured. Revert = `CONVICTION_FLOOR=0`, `MIN_MULT=1` (no-op).

## 7. Risks
Decider confidence may be poorly calibrated (it's an LLM self-report). **Mitigation:** first *measure* confidence↔outcome correlation in F1; only enable scaling if the signal is real — otherwise this just adds variance. Treat F5 as gated on that evidence.

# FRD F4 — Bigger-Move Strategy Reframe

_Phase 2 · Owner: house engineer loop + PM · Status: proposed · Depends on: F1 (measurement), F2 (gate), #681 (EOD-tighten)_

## 1. Problem
Strategies scalp ~₹31 (~0.25%) moves where a fixed ~₹13 cost eats 43%. Even a positive *gross* edge can't survive that ratio. We must capture materially bigger moves per trade and trade less often.

## 2. Goal & success metrics
Reframe every strategy + the league mandates around **≥0.6–0.8% target moves** and fewer, higher-quality entries.
- Driver: realised E2C ≥ 3 (F1); median target distance ≥ `MIN_TARGET_PCT`; trades/day ↓.
- Outcome: net payoff ratio approaches the gross ~2.3:1; net expectancy ≥ 0 over 30 trades.

## 3. HLD
Two levers, both measured via F1's A/B:
1. **Target floor** — every emitted Signal's target must be ≥ `entry × (1 + MIN_TARGET_PCT)` (long) / ≤ `entry × (1 − MIN_TARGET_PCT)` (short). Strategies widen their natural target up to the floor (and widen the stop proportionally to keep RR sane, or accept a higher RR).
2. **Fewer, better entries** — tighten triggers so we fire only on stronger setups; lean on the existing `max_signals_per_symbol_per_day` / `per_symbol_cooldown_min` and lower them.

```
strategy.scan() ─► natural (entry, stop, target)
                 ─► target' = enforce_min_move(entry, target, side, MIN_TARGET_PCT)
                 ─► RR check vs risk gate (1.5) ─► Signal
```

## 4. LLD
**Config (`helm/config.py`):**
```python
MIN_TARGET_PCT = Decimal("0.006")     # >= 0.6% gross target move on the entry price
MAX_TRADES_PER_DAY_PER_SYMBOL = 2     # was max_signals_per_symbol_per_day (lower it)
```
**Shared helper** (`helm/strategies/base.py` or a small `helm/strategies/_moves.py`):
```python
def enforce_min_move(entry: Decimal, target: Decimal, side: str, min_pct: Decimal) -> Decimal:
    floor = entry * (Decimal(1) + min_pct) if side == "BUY" else entry * (Decimal(1) - min_pct)
    return max(target, floor) if side == "BUY" else min(target, floor)
```
- Apply in each strategy's `scan` right before constructing `Signal` (ORB, bbands_zscore_20, vwap_reclaim, gap_fade). For mean-reversion (target = the mean), if the mean is closer than the floor, **widen to the floor** OR **drop the signal** (config flag `MEANREV_WIDEN_OR_DROP`) — widening a reversion target past the mean changes the thesis, so default = **drop** for reversion, **widen** for momentum.
- Keep stop logic; the RR floor in each strategy + `risk.evaluate` (1.5) still applies. F2's gate then guarantees E2C as well.
- Lower `RiskLimits.max_signals_per_symbol_per_day` and raise `per_symbol_cooldown_min`.

**Interaction with #681:** bigger targets ⇒ more positions still open near EOD ⇒ the EOD-approach stop-tighten (shipped) protects the give-back — these are designed to work together.

## 5. Test plan
`tests/test_min_move.py`: floor widens a too-tight momentum target; reversion signal with sub-floor mean is dropped (or widened per flag); existing wide targets pass unchanged; RR still ≥1.5 after widening. Per-strategy unit tests asserting emitted target respects the floor.

## 6. Rollout / revert
A/B: enable per-strategy via the existing ACTIVE roster and compare E2C/expectancy in F1 over ≥2 weeks before trusting defaults. Revert = `MIN_TARGET_PCT=0`, restore signal caps.

## 7. Risks
Bigger targets → lower hit rate and longer holds (EOD risk — mitigated by #681). Drop-vs-widen for reversion changes signal frequency — measure both arms.

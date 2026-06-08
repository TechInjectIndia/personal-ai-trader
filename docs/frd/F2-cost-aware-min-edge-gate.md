# FRD F2 — Cost-Aware Minimum-Edge Gate

_Phase 1 · Owner: house engineer loop · Status: proposed · Depends on: charges.round_trip_breakdown (exists)_

## 1. Problem
We open trades whose target move is smaller than a few × the round-trip cost. With ₹13 cost on a ₹31 move, sub-3× setups are mathematically guaranteed to lose net even when right. These scalps are the bulk of the bleed.

## 2. Goal & success metrics
Refuse to open any (house) trade whose expected gross reward is below a configurable multiple of its expected round-trip cost.
- Driver metric: **0 opened house trades with E2C < `MIN_EDGE_TO_COST`**.
- Outcome metric (via F1): net expectancy/trade trends up; trades/day down; cost-drag % down.

## 3. HLD
A deterministic gate at the **single house execution chokepoint** `scripts/paper_execute.execute_signal`, evaluated *after* sizing (qty known) and *before* `risk.evaluate` opens the trade. Computes expected round-trip cost for the sized position and the gross reward to target; blocks (records a `SKIP`) when the ratio is too thin.

**League scope:** NOT enforced on competitors (autonomy). Instead surfaced to their loops as advice via F8 / mandates. House-only, exactly like #681's `_is_house` gate.

```
signal (entry, target, qty) ─► expected_cost = round_trip_breakdown(side, qty, entry, target).total
                               gross_reward  = |target - entry| * qty
                               E2C = gross_reward / expected_cost
                               E2C < MIN_EDGE_TO_COST ?  ── yes ─► SKIP (reason='below_min_edge_to_cost')
                                                          └ no ──► risk.evaluate → open
```

## 4. LLD
**Config (`helm/config.py`)**, module-level beside `DYNAMIC_CAP_*`:
```python
MIN_EDGE_TO_COST = Decimal("3.0")   # require gross target reward >= 3x expected round-trip cost
```
**`scripts/paper_execute.execute_signal`** — after `sized_qty`/`entry` resolved and before `risk.evaluate`:
```python
if target is not None and sized_qty > 0:
    exp_cost = round_trip_breakdown(side, sized_qty, entry, target).total
    gross_reward = abs(target - entry) * sized_qty
    e2c = (gross_reward / exp_cost) if exp_cost > 0 else Decimal("0")
    if e2c < MIN_EDGE_TO_COST:
        # record SKIP decision + audit, mark signal consumed, return ExecutionResult(False, ...)
        return _skip(signal_id, actor, f"below_min_edge_to_cost (E2C={e2c:.2f})", ...)
```
- Reuses the existing `round_trip_breakdown` (`helm/charges.py`) — same cost model the P&L uses, so the gate and the books agree.
- `target is None` → skip the gate (let existing logic handle; or treat as block — default: skip-gate, since a target-less signal can't be E2C-evaluated). Document the choice.
- Emits a `decisions` row with `verdict='SKIP'`, `reasoning` carrying the E2C, and flips `consumed` in the same transaction (mirror the existing risk-block path so it's never reprocessed).

**Decider awareness (optional, cheap):** add the computed E2C to the decider prompt context so the LLM also "knows" cost — but the deterministic gate is the enforcement.

## 5. Test plan
`tests/test_min_edge_gate.py`: (a) thin target (E2C<3) → SKIP, no `paper_trades` row, `decisions.verdict='SKIP'`, signal consumed; (b) fat target (E2C≥3) → proceeds to risk.evaluate; (c) target None → gate skipped; (d) exp_cost==0 guard. Pure where possible (monkeypatch `round_trip_breakdown` / DB).

## 6. Rollout / revert
Tune `MIN_EDGE_TO_COST` conservatively (start 2.5–3.0); watch trades/day for starvation. Revert = set `MIN_EDGE_TO_COST = Decimal("0")` (gate becomes a no-op) — instant kill switch, no redeploy of logic.

## 7. Risks
Over-tight threshold starves the book (mitigation: tunable, monitor). Cost model drift vs reality (mitigation: it's the *same* model as the realized charges, so consistent by construction).

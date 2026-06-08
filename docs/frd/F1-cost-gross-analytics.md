# FRD F1 — Cost & Gross-Expectancy Analytics

_Phase 1 · Owner: house engineer loop · Status: proposed · Prereq for: F2, F4, F5, F6, F8_

## 1. Problem
The self-improvement loop optimizes **net** P&L but is blind to the gross/cost split. The headline truth — gross ≈ flat (−₹160), costs (−₹2,463) are the whole loss — is invisible to retros, PM, and the dashboard. We cannot manage the edge-to-cost ratio we can't see.

## 2. Goal & success metrics
Make these first-class, queryable, and surfaced everywhere the loop reasons:
- Gross expectancy/trade, net expectancy/trade, **cost drag %**, **realised E2C** (`|gross move| / cost`), win%, gross & net payoff — sliced by `strategy`, `competitor_id`, `symbol`, `exit_reason`, and trailing-N window.
- Success: weekend/daily report shows the table; retro & PM prompts include each agent's economics; dashboard page renders it.

## 3. HLD
A pure read-only analytics module over `paper_trades` (+ `decisions`→`signals` for strategy attribution). No new trading behavior. Three consumers:
1. `weekend_report.py` — new "Unit economics" section.
2. Retro/PM input context (`helm/agents/*`) — per-agent economics injected so proposals target the real lever.
3. Dashboard — a panel on the existing P&L / Self-Improvement pages.

```
paper_trades ─┬─ pnl_inr (gross) ─┐
              ├─ charges_inr      ├─► economics.py ─► report / retro-input / dashboard
              └─ net_pnl_inr ─────┘
decisions→signals.strategy ──────► attribution
```

## 4. LLD
**New module `helm/analytics/economics.py`** (no DB writes):
```python
@dataclass(frozen=True)
class Economics:
    n: int
    gross_pnl: Decimal; charges: Decimal; net_pnl: Decimal
    gross_expectancy: Decimal; net_expectancy: Decimal
    cost_drag_pct: Decimal          # charges / abs(gross_pnl) * 100
    win_pct: Decimal; gross_payoff: Decimal; net_payoff: Decimal
    avg_abs_gross_move: Decimal; realised_e2c: Decimal  # avg|gross move| / avg charge

def book_economics(*, group_by: str | None = None,   # 'strategy'|'competitor_id'|'symbol'|'exit_reason'
                   competitor_filter: str | None = None,
                   since: datetime | None = None,
                   last_n: int | None = None) -> dict[str, Economics] | Economics
```
- Single SQL with `GROUP BY`; reuse the test-row exclusion `competitor_id IS NOT NULL AND competitor_id NOT LIKE 'zzz%'` (and `HOUSE_TRADE_FILTER` for house views). Strategy attribution via `JOIN decisions d ON d.id=pt.decision_id JOIN signals s ON s.id=d.signal_id`.
- All-Decimal; `cost_drag_pct`/`realised_e2c` guard divide-by-zero → `Decimal('0')`.

**`weekend_report.py`** — add `_unit_economics_section(c)` calling `book_economics(group_by='strategy')` and `(group_by='competitor_id')`; render a table (gross / cost / net / E2C / payoff). Insert after the existing P&L snapshot.

**Retro/PM input** — in `helm/agents/pm.py` (and the retro input builder), inject `book_economics(competitor_filter=cid, last_n=30)` into the prompt context block so each agent's PM sees "gross expectancy −₹2/trade, cost drag 1500%, E2C 2.3" and proposes the right lever.

**Dashboard** — new section on `helm/dashboard/pages/7_Self_Improvement_Loop.py` (or 2_Summary) using `book_economics`; reuse `kpi_grid`/`kpi_card`.

**No schema change** — `pnl_inr`, `charges_inr`, `net_pnl_inr` already exist.

## 5. Test plan
`tests/test_economics.py` — feed synthetic closed trades (DB-free by passing rows, or a seeded test DB) and assert each field; specifically a case where gross>0 but net<0 (cost drag) and E2C math. Edge: zero trades, zero gross (div-guard).

## 6. Rollout / revert
Pure additive read-only module. Ship behind nothing; revert = remove the report section + import. No live-bot risk.

## 7. Risks
Strategy attribution join misses trades with NULL decision_id → report those as `unattributed` rather than dropping.

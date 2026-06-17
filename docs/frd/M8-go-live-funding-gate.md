# FRD M8 — Per-Market Go-Live Funding Gate

_Multi-Market · Phase 4 · Owner: human (judge) + house engineer loop · Status: proposed · Depends on: M2 (per-market books), M5 (backtest), M6/M7 (markets live in paper)_

## 1. Problem
The whole program exists to answer one question per market: **"is this making money, repeatably, before we risk real capital?"** Without an explicit, data-driven gate, the temptation is to fund on a hunch. The live IN book (net-negative on a thin, cost-eaten edge) is the cautionary case: paper looked busy, economics were losing.

## 2. Goal & success metrics
A single, auditable gate that says — per market — whether the evidence justifies real money. Nothing places a live order until its market passes. The human pulls the trigger; the gate provides the verdict and the evidence.
- Driver metric: **0 markets funded without a passing gate row.**
- Outcome: a `go_live_readiness` view/report per market that the dashboard surfaces and the human signs off on.

## 3. HLD
A read-only evaluator that combines the two independent evidence streams already produced — **forward paper** (live `paper_trades`, per-market book) and **backtest** (`backtest_runs`, M5) — into a pass/fail with reasons. Funding stays a **manual** human action (no auto-trading-with-real-money — consistent with the "human triggers, agents decide within paper" model and the computer-use financial-action guardrail).

```
per market:
  paper_gate    = net_expectancy/trade > 0 over trailing N closed trades  (N≥30)
                  AND win/payoff sane AND cost_drag% within bound AND ≥ K trading days
  backtest_gate = M5 net expectancy > 0 over the deepest available window,
                  net of THAT market's cost model, on the same strategy set
  READY = paper_gate AND backtest_gate AND kill-switch never tripped in window
  → surfaced to Action Center; human funds manually if READY
```

## 4. LLD
**`scripts/go_live_readiness.py`** (read-only, no writes to the trade path):
- Reuses `helm/eval/metrics.summarize` over (a) the market's live closed `paper_trades` (trailing N, per-market `HOUSE_TRADE_FILTER` + `market=`) and (b) its latest `backtest_runs` rows.
- Thresholds (code-is-config, per market — crypto stricter because fees are higher):
  ```python
  READINESS = {
    "IN":     Gate(min_trades=30, min_net_exp=Decimal("0"), max_cost_drag=Decimal("0.35"), min_days=10),
    "US":     Gate(min_trades=30, min_net_exp=Decimal("0"), max_cost_drag=Decimal("0.20"), min_days=10),
    "CRYPTO": Gate(min_trades=30, min_net_exp=Decimal("0"), max_cost_drag=Decimal("0.50"), min_days=14),
  }
  ```
- Emits a `go_live_readiness` row (`market, evaluated_ts, paper_pass, backtest_pass, ready, metrics_json, reasons`) and pushes a summary to the **Action Center** (`helm/...attention.enqueue`) when a market first flips READY.
**Dashboard:** a per-market readiness card on the Self-Improvement / Overview surface (read-only, like everything else) — green only when both streams pass; shows the blocking reason otherwise.
**Funding action:** documented runbook only — the human enables live execution for a market by (1) provisioning the broker (Kite/Alpaca/exchange), (2) setting that market's live flag, (3) starting with the smallest real notional. **No code path auto-arms live trading.**
**Continuous re-check:** the readiness script runs on the daily post-close cadence (reuse F3 timing) so a market that *was* READY but degrades flips back to not-ready and re-alerts (the give-back guard at the portfolio level).

## 5. Acceptance
- A market with a losing trailing-30 paper book reports `ready=false` with the specific failing reason — even if its backtest passes (both gates required).
- The IN book today evaluates to **not ready** (matches reality — it's net-negative), proving the gate isn't a rubber stamp.
- No script in the repo can place a real order purely from a READY verdict (grep: live-arm is manual/flag-gated only).
- Readiness re-evaluates daily and flips back to not-ready on degradation.

## 6. Out of scope / guardrails
No auto-funding, no auto-sizing of real capital. No portfolio-level capital allocation across the three markets (each is funded independently; a cross-market allocator is a future concern). FX/portfolio consolidation is dashboard-only and does not affect the per-market gate. The gate is necessary, not sufficient — the human judge retains veto and starts every funded market at minimum size.

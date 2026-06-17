# FRD M5 — True Historical Backtester (decider-off)

_Multi-Market · Phase 1 · Owner: house engineer loop · Status: proposed · Depends on: M1, M3, M4 · Reuses: helm/eval/{replay,metrics}.py_

## 1. Problem
Today's "backtest" (`helm/eval/backtest.py`) re-prices **already-booked** `paper_trades` over recorded `candles_1m` — it validates exit-policy changes for the eval-gate, but it cannot evaluate a strategy on history it never traded, nor on a brand-new market with no booked trades. We need a forward backtest: feed historical OHLC → run `Strategy.scan()` bar-by-bar → size + cost → simulate exits → metrics. Across months, running the LLM decider is expensive and non-deterministic, so backtests must run **decider-off**.

## 2. Goal & success metrics
A deterministic, decider-off backtester that measures **raw strategy edge net of the market's costs**, reusing `simulate_trade()` and `metrics.py`.
- Driver metric: same (strategy, market, window, seed) → **identical** metrics on re-run (deterministic).
- Outcome: every strategy in `ACTIVE` has a backtest report per market (net expectancy/trade, win%, E2C, max DD, trades/day) — the evidence the M8 funding gate consumes.

## 3. HLD
A new `helm/eval/forward.py` that walks historical candles through the pure strategy, then through the *existing* simulator and cost model.

```
m.data.historical(sym, bar_minutes, range)         # M3 feed (ccxt years / yfinance ~60d)
   └─► for each bar t: strategy.scan(sym, candles[:t+1])  -> Signal | None
         └─► size (market.fractional sizing) + risk caps (reuse risk.evaluate logic, no DB)
               └─► simulate_trade(side, entry, stop, target, qty, candles[t+1:], cost_model=m.costs)
                     └─► SimOutcome  ── collect ──► helm/eval/metrics.summarize()
DECIDER: OFF (take every gated signal) | optional "rule decider" stub. NEVER the LLM.
```

The decider is replaced by a pluggable `BacktestDecider`: default `TakeAll` (measures raw strategy+risk+cost edge); optional deterministic heuristics for A/B. The LLM is explicitly disallowed here (cost + reproducibility).

## 4. LLD
**`helm/eval/forward.py`:**
- `backtest(strategy, market, symbol, start, end, *, decider=TakeAll(), seed=0) -> BacktestReport`.
- Pulls candles via `market.data.historical`; resamples to `strategy.bar_minutes` (reuse `scan_signals`'s resample helper — extract if inline).
- For each emitted Signal: apply the **same gates** as live in pure form — `MIN_EDGE_TO_COST`/F2 (using `market.costs`), `MIN_TARGET_PCT`/F4, sizing caps — by factoring the gate math out of `paper_execute` into a pure `helm/eval/sizing.py` shared by live + backtest (no DB).
- Exits via `helm.eval.replay.simulate_trade` with the market cost model (M4) and the calendar's square-off (`None` ⇒ no EOD flat — crypto runs trades to stop/target only).
- **No look-ahead:** `scan` only sees `candles[:t+1]`; fills use `candles[t+1:]`. Pin this with a test.
**`BacktestReport`** persisted to a new `backtest_runs` table (`id, strategy, market, symbol, start, end, params_json, metrics_json, code_sha, created_ts`) — kept **separate** from live `paper_trades` so backtests never pollute the live book or the eval-gate's window.
**CLI `scripts/run_backtest.py`:** `--strategy orb --market CRYPTO --symbol BTC --days 180`; prints the metrics table and writes a `backtest_runs` row. Sits beside the existing `scripts/replay_backtest.py` (which keeps its eval-gate role).
**Determinism:** no `Date.now`/random; `seed` only matters if a heuristic decider samples. Cost/sizing are pure Decimal.

## 5. Acceptance
- Re-running the same backtest yields byte-identical `metrics_json`.
- A look-ahead probe (strategy that "peeks" at a future bar) is structurally impossible — the slice boundary test fails the build if violated.
- On the IN market, a backtest of a strategy over a window where it *did* trade live produces metrics directionally consistent with the live book (sanity, not exactness — live had the LLM decider).
- `backtest_runs` rows never appear in `closed_trades()`/eval-gate queries (book isolation).

## 6. Out of scope / guardrails
No LLM in backtests (hard rule — cost + determinism). No tick-level/partial-fill microstructure modeling (1-min close fills, same fidelity as the live paper engine). No portfolio-level cross-symbol optimization — per (strategy, symbol) reports, aggregated by the report layer. Historical-data depth is bounded by the source (yfinance ~60d 1m for IN/US; ccxt deep for crypto) — the report records the actual window so a thin IN backtest isn't mistaken for a deep one.

# Agent-Fleet Capability Spec (Multi-Market Go-Live)

_Step 1 of the autonomous build. Translates `.claude/prds/multi-market-go-live.prd.md`
(PRD + North Star) into an implementation-ready capability map: the agent
action-space, the interfaces, the invariants that must never break, and the data
contracts that feed the M8 funding gate. Design artifact — no code._

## 1. The unit: an agent

An **agent** is one trading identity = **`competitor_id` × `market`**, carrying a
**persona** and a **mandate**. It owns an isolated book (wallet + positions +
P&L) and an independent learning loop. The fleet is the grid of
**agent × persona × market × strategy** combinations.

| Axis | Where it lives today | Gap to close |
|---|---|---|
| market | `helm.markets` (M1–M7): IN, CRYPTO, US registered | commodities = future registration |
| persona | `competitors.persona` | persona→behaviour wiring is shallow (mostly prompt) |
| mandate (universe + strategy_config) | `competitor_mandates` | per-market mandates (today NSE-centric) |
| strategy | `helm.strategies.ACTIVE` + `session_required` allowlist | per-market strategy seeding |

## 2. The action-space (what an agent decides)

Each agent, per tick, chooses within hard bounds:
1. **Entity** — a symbol from its market's watchlist / mandate universe.
2. **Strategy** — which `Strategy.scan` rule fires (seeded from industry
   standards; session strategies barred on 24/7 venues).
3. **Signal → act?** — the decider (LLM or rule) says TAKE/SKIP on the emitted `Signal`.
4. **Size** — within the per-market cap + wallet + cost gate (never free choice).

**Bounded by (the guardrail stack, in order):** cost gate (F2 edge-to-cost,
per-market `CostModel`) → risk gate (`risk.evaluate(market=…)`: kill switch,
daily-loss, position/cooldown/signal caps, per-trade cap, wallet) → **(S4)**
trading-safety guard (spend ceiling, pre-trade sim, circuit breaker) → execution
(`paper_execute`, the single chokepoint). No path bypasses this stack.

## 3. Interfaces

**Exist (M1–M8):**
- `Market(key, data, calendar, costs, currency, fractional, watchlist, max_hold_min)` — `helm/markets`.
- `Strategy.scan(symbol, candles) -> Signal | None` — pure; `session_required` flag.
- `risk.evaluate(symbol, side, qty, entry, competitor_id, strategy, market)` — per-market scoped.
- `wallet_state(market)` — per-market book (IN via `live_wallet_config`, others via `wallets`).
- `CostModel.round_trip_breakdown(...)` — Zerodha / AlpacaEquity / CryptoBps.
- `forward.backtest(strategy, market, symbol, …)` → `backtest_runs`.
- `go_live_readiness.evaluate_market(market)` → readiness verdict (M8).

**To build (this run):**
- **S2** `config.MARKET_RISK` + `live_risk_limits_for(market)` — currency-correct caps.
- **S3** an eval harness wrapping `go_live_readiness` with reproducible per-market metrics + reasons.
- **S4** `helm/safety.py` — pre-trade spend/notional ceiling, simulation hook, circuit breaker, decider-input sanitisation.
- **S5** per-(agent, market) lessons surface generalizing `agent_instincts` / retrospectives.
- **S6** `scripts/run_backtest_sweep.py` — drive backtests across enabled markets → `backtest_runs`.

## 4. Invariants (must always hold — the review gate enforces these)

1. **IN byte-identical** — every change defaults to the prior NSE behaviour
   (`market="IN"`, `live_risk_limits()`, IST boundary). Proven by the 286-test suite.
2. **Currency isolation** — no query sums P&L across currencies; each book is
   reported in its own currency (INR/USD).
3. **Book isolation** — `HOUSE_TRADE_FILTER` + `market` scope every count/sum; one
   agent's trades never leak into another's accounting.
4. **One decision per signal** — every signal → exactly one `decisions` row
   (TAKE/SKIP incl. gate blocks) → `consumed=TRUE`.
5. **No auto-arm of live capital** — code only ever paper-trades; live funding is a
   manual human action gated on M8 READY. (Step 9 / deploy is human-only.)
6. **Deterministic backtests** — same (strategy, market, window, seed) → identical
   metrics; no LLM, no clock, no RNG in the backtest path.
7. **Fail-soft data** — a data/adapter outage returns None/[] and is audited; it
   never crashes a cron tick.

## 5. Data contracts feeding the M8 funding gate

```
paper_trades(market, competitor_id, status, pnl_inr, charges_inr, net_pnl_inr, exit_ts)
        │  (forward paper evidence: trailing-N net expectancy, cost drag, days)
backtest_runs(market, strategy, symbol, metrics jsonb, n_trades)
        │  (backtest evidence: net per (strategy,symbol))
agent_instincts / trade_retrospectives(competitor_id[, market])
        │  (lessons learned — informs persona/strategy, not the gate directly)
        ▼
go_live_readiness(market, paper_pass, backtest_pass, ready, metrics, reasons)
        ▼  READY  →  Action Center  →  HUMAN funds at minimum size (manual)
```

The gate is **necessary, not sufficient**: it produces the verdict + evidence;
the human retains veto and sizes the first real capital.

## 6. Buildable units → steps

| Unit | Step | Acceptance (summary) |
|---|---|---|
| Currency-correct caps | S2 | US/CRYPTO sized in USD; IN suite green |
| Reproducible funding gate | S3 | verdict deterministic; losing/thin books fail with reasons |
| Pre-capital safety stack | S4 | spend ceiling + circuit breaker + injection-safe inputs, all tested |
| Cross-market lessons | S5 | lesson retrievable per (agent,market); decay/reopen works; IN preserved |
| Evidence sweep | S6 | `backtest_runs` populated per (strategy,market,symbol); feeds gate |
| Adversarial review | S7 | 0 CRITICAL/HIGH; invariants §4 hold; suite green |

## 7. Open risks (carry into the build)

- **Persona depth** — persona is mostly a prompt today; making aggressive/balanced/
  defensive *mechanically* shape sizing/strategy selection is larger than this run
  (flagged, not built here).
- **USD cap calibration** — S2 sets placeholder USD caps; the operator tunes before funding (PRD open question).
- **Crypto edge vs fees** — already evidenced (F2 gates thin crypto targets); the
  gate refusing to fund crypto is *correct behaviour*, not a bug to "fix".
- **Live execution wiring** — explicitly out of scope until a market passes the gate.

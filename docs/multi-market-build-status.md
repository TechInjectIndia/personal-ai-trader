# Multi-Market Build — Status & Handoff (M1–M8)

_Built 2026-06-17 on branch `feat/multi-market` (NOT pushed, NOT deployed). All
8 FRDs implemented + tested. India (IN) is byte-identical and the only market
enabled by default; US + CRYPTO are registered but dark._

## What landed (one commit per FRD)

| FRD | What | Key files |
|---|---|---|
| M1 | `Market` abstraction + registry; IN poll/manage routed through it | `helm/markets/*` |
| M2 | Schema namespacing (`market`), per-market wallets, fractional `qty` | `helm/data/schema.sql`, `scripts/migrate_multimarket.py`, store/wallet/risk/paper_execute |
| M3 | Data adapters (ccxt, Alpaca, yfinance-US) + calendars (24/7, NYSE) | `helm/markets/{data,calendars}.py` |
| M4 | Per-market cost models (US SEC+TAF, crypto taker-bps) | `helm/markets/costs.py` |
| M5 | Forward backtester (decider-off, deterministic) + `backtest_runs` | `helm/eval/forward.py`, `scripts/run_backtest.py` |
| M6 | Crypto enablement (24/7, fractional, time-stop, session allowlist) | `helm/markets/registry.py`, scan/manage |
| M7 | US enablement (NYSE calendar, market-tz "today" boundary) | registry, store/risk tz |
| M8 | Per-market go-live funding gate (read-only) | `scripts/go_live_readiness.py` |

Tests: **286 passed, 10 skipped** (`ruff` clean). The skips are the known-stale
`test_risk.py` + integration tests that only run against an isolated schema.

## ⚠️ Deploy order (IMPORTANT)

The new code **requires the M2 migration** (it references the `market` column and
the new candle PK). Deploy is **migrate, then ship code**:

1. `python scripts/migrate_multimarket.py` against the live `helm` DB. It is
   additive + idempotent; every legacy row defaults to `market='IN'`, so the
   India book is byte-identical afterwards. (The `ALTER COLUMN qty TYPE NUMERIC`
   briefly locks `paper_trades` — run it **after market close**.)
2. Merge `feat/multi-market` → your live branch and `pm2 restart helm-dashboard`.

Running the new code against an **un-migrated** DB will error (by design).

## How to enable a new market (paper)

1. Set `MARKET_ENABLED["CRYPTO"] = True` (or `"US"`) in `helm/config.py`; reload.
2. Crypto trades 24/7 → add UTC, weekend-inclusive cron lines (the IST-windowed
   ones stay for IN; each script no-ops outside its own market's calendar):
   ```cron
   # crypto: poll/scan/manage every minute, 24/7
   * * * * * /…/scripts/run_in_venv.sh scripts/poll_market.py
   * * * * * /…/scripts/run_in_venv.sh scripts/scan_signals.py
   * * * * * /…/scripts/run_in_venv.sh scripts/manage_positions.py
   ```
   (US runs inside the existing minute cron — `NYSECalendar` gates it to ET hours.)
3. Backtest first: `python scripts/run_backtest.py --strategy bbands_zscore_20 \
   --market CRYPTO --symbol BTC --days 180 --base-cap 1000 --persist`.
4. Watch readiness: `python scripts/go_live_readiness.py` (wire to the daily
   post-close cron alongside the loop). A market turns READY only when **paper
   AND backtest** are positive — then it surfaces in the Action Center.
5. **Fund manually, smallest size first.** Nothing arms live trading in code.

## Known simplifications (POC; safe because non-IN is paper-only + dark)

- **Per-market risk caps** still use `max_position_inr` (INR-scaled) as the base
  cap; for US/crypto the USD wallet (`wallets` table: CRYPTO \$1k, US \$5k) is
  the binding constraint. Set proper per-market caps before funding.
- **Backtest exits** are evaluated at the strategy's bar granularity (not 1-min)
  — a deliberate POC fidelity trade-off, recorded in `helm/eval/forward.py`.
- **Resample anchor** for N-min bars is `market-midnight + 9h15m` (exact 09:15
  for IN; ~session-open for US/crypto). Day-boundary is correct per market.
- **Live US/crypto execution** is out of scope (M8 is paper→fund-decision only):
  wiring a US broker (Alpaca) / crypto exchange is a later, separate step.

## Test infra note

Integration tests run against an isolated Postgres schema, not live data:
```bash
psql -d helm -c "CREATE SCHEMA IF NOT EXISTS mm_test"
HELM_SEARCH_PATH=mm_test PYTHONPATH=. python scripts/migrate_multimarket.py
HELM_SEARCH_PATH=mm_test PYTHONPATH=. pytest          # 286 passed
```
`helm.data.store.conn` honors `HELM_DSN` / `HELM_SEARCH_PATH` env overrides
(unset in production → behaviour unchanged).

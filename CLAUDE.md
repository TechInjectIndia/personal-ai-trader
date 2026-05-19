# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo actually is

Single-user **intraday paper-trading bot** for the top-5 NSE large caps (RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS). Cron-driven Python scripts read market data, generate signals, ask Claude (Anthropic API) whether to take each one, and book paper trades in Postgres. A Streamlit dashboard surfaces state.

**The README is out of date.** It describes the original PRD (Sleeve A passive / Sleeve B intraday, IBKR, dry-run gates G1–G4, `backtrader`). That work was scoped out. The current code has no IBKR, no passive sleeve, no `backtrader`, no `helm.backtest`, no `helm.orchestrator.allocator`/`drift_detector`/`intraday_gate`/`sleeve_halt`. Don't reintroduce those modules unless explicitly asked. The Makefile still has `dry-run-*` and `backtest` targets that point at deleted files — treat those as dead.

## Runtime architecture

Four cron jobs drive everything (`crontab -l` to inspect; wrapper `scripts/run_in_venv.sh` activates `.venv` and runs the given script). All four no-op outside their IST window and on weekends.

| Cron cadence (UTC) | Script | Role |
|---|---|---|
| every minute, 03:45–10:00 UTC Mon–Fri | `scripts/poll_market.py` | Pull LTP for each `WATCHLIST` symbol via `yfinance` → `ticks` table → `roll_minute_candles()` upserts `candles_1m` |
| every minute | `scripts/scan_signals.py` | Iterate `helm.strategies.ACTIVE` × `WATCHLIST`; emit unconsumed `signals` rows AND inline-decide each new one via `scripts.decide_signals.decide_signal_inline` (synchronous, ~5–10s end-to-end). This collapses scan→decide→paper-trade into one pipeline. |
| every 2 min | `scripts/decide_signals.py` | Catch-up safety net: re-decides any unconsumed signals that the inline path missed (LLM down, etc.). Auto-SKIPs anything older than `STALE_THRESHOLD_MINUTES=20` without an LLM call. |
| every minute | `scripts/manage_positions.py` | Walk OPEN `paper_trades`; close on stop/target hit, or EOD square-off at `SQUARE_OFF_AT` (15:15 IST) |

`paper_execute.execute_signal` is the single chokepoint that calls `helm.orchestrator.risk.evaluate` before opening any trade. Every signal flows through one `decisions` row (TAKE or SKIP, including risk-gate blocks) and gets `consumed = TRUE` so it's never reprocessed.

Per-trade notional is `min(dynamic_position_cap(realised_pnl, base_cap), wallet.available) // entry_price`. The dynamic cap starts at `RiskLimits.max_position_inr` (₹15k default) and adds ₹1.5k per ₹5k of realised profit, capped at ₹25k — so winners compound but losers don't shrink the cap below baseline.

The Streamlit dashboard (`helm/dashboard/app.py`) is **read-only over Postgres** plus a Kite `profile()`/`margins()` call. It runs under PM2 (`ecosystem.config.js`) on `127.0.0.1:8501` behind nginx + LE at `https://helm.techinject.co.in`.

## Key data model facts

Schema in `helm/data/schema.sql`. Tables: `ticks`, `candles_1m`, `signals`, `decisions`, `paper_trades`, `audit`, `daily_state`, `settings`, `trade_retrospectives`, `improvement_proposals`. All timestamps are `timestamptz`; the code consistently uses `Asia/Kolkata` (`IST = ZoneInfo("Asia/Kolkata")`) for "today" boundaries via `date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'`. Reuse that idiom for any "today" query.

`improvement_proposals` is the "learning from mistakes" pipeline: every retrospective produces 0–3 concrete change ideas (category ∈ strategy / decider_prompt / risk / sizing / execution / data / meta) that get persisted as their own rows. `scripts/proposals_digest.py` aggregates them across retros, ranks by recurrence × confidence, and supports a status workflow (`open` → `accepted` → `applied`, or `rejected`/`superseded`). When you're picking the next change to make, start there.

`agent_tasks` / `releases` / `agent_runs` / `metrics_snapshots` power the **autonomous self-improvement loop**: three cooperating agents (PM → Engineer → Tester) that turn `improvement_proposals` into shipped, verified changes. See `AGENTS/Product-Manager-Agent.md`, `AGENTS/Product-Engineer-Agent.md`, `AGENTS/Tester-Agent.md` for charters. Cadence: PM Mondays 06:00 IST, Engineer every 30 min weekdays, Tester reactive (every 5 min, no-op when no unverified releases). Engineer commits locally on `main`; the human pushes to the remote. Live state on the dashboard's "Self-Improvement Loop" page. To pause: `INSERT INTO settings (key,value,updated_by) VALUES ('autonomy_paused','true','manual')`.

Connection helper is `helm.data.store.conn()` — autocommit, `dict_row` factory, DSN is `"dbname=helm"` (local Unix socket as current OS user). Don't add a `pool` or async layer; this is a small single-user bot.

## Configuration model

`helm/config.py` is the **single source of truth** for runtime config — watchlist, trading window, risk limits (`RISK = RiskLimits(...)`), polling cadence, decider model. Hardcoded by design; edit and reload PM2 to change. Don't introduce a YAML/JSON config file.

`.env` holds only secrets and per-environment values: `KITE_API_KEY/SECRET/ACCESS_TOKEN`, `ANTHROPIC_API_KEY`, optional `DECIDER_MODEL` override. `KITE_ACCESS_TOKEN` is regenerated daily via `scripts/kite_login.py` (Kite tokens expire at 06:00 IST).

## Important constraint: Kite market-data add-on missing

This account's Kite Connect app does **not** have the historical/quote market-data add-on. `kite.ltp()`, `kite.quote()`, `kite.ohlc()`, `kite.historical_data()` all raise `PermissionException` (403). That's why `poll_market.py` and `manage_positions.py` use `yfinance` (`yf.Ticker(f"{symbol}.NS").fast_info.last_price`) instead of Kite quotes. Don't "fix" this by switching to Kite calls — they will 403.

Order placement, profile, and margins endpoints DO work with the base subscription, so the dashboard's `kite.profile()`/`kite.margins()` and the dry-run order placement still function.

## Strategy interface

`helm/strategies/base.py` — `Strategy` ABC with one method `scan(symbol, candles) -> Signal | None`. Pure function: in 1-min OHLC dicts, out a candidate `Signal`. **No DB access from strategies.** Active set in `helm.strategies.__init__.ACTIVE`. To add a strategy: subclass, append to `ACTIVE`, done. Strategies must fire only on the breakout/trigger bar (see `orb.py` — checks prior bar didn't already break out) so re-running `scan_signals.py` every 5 minutes doesn't re-emit.

## Decider details (Anthropic API)

`scripts/decide_signals.py` uses the `anthropic` SDK with **prompt caching on the system prompt** (`cache_control: ephemeral`). Default model is `claude-sonnet-4-6`; override via `DECIDER_MODEL` env var. Output is strict JSON `{verdict: TAKE|SKIP, confidence, reasoning}`. Tolerant parser strips ``` fences. On API error or malformed JSON: log to `audit`, leave signal unconsumed for retry. Trading window guarded by `_within_window` (TRADING_START–TRADING_END IST, weekdays only); `--force-window` bypasses for manual runs.

If you change the system prompt, expect the cache hit to drop on the first run after the change.

## Common commands

```bash
# Activate venv (every shell)
source .venv/bin/activate

# Schema bootstrap (idempotent)
python -c "from helm.data.store import init_schema; init_schema()"

# Manual one-shot runs (bypass cron)
python scripts/poll_market.py
python scripts/scan_signals.py
python scripts/decide_signals.py --force-window           # all unconsumed today
python scripts/decide_signals.py --signal-id 42 --force-window
python scripts/paper_execute.py --signal-id 42 --actor manual --reasoning "..."
python scripts/manage_positions.py

# Daily Kite token refresh (token expires 06:00 IST)
python scripts/kite_login.py <request_token>

# Dashboard (also runs under PM2)
streamlit run helm/dashboard/app.py
pm2 restart helm-dashboard      # after editing dashboard or config.py
pm2 logs helm-dashboard

# Tests / quality (ruff line-length 100, py311 target)
pytest                            # NOTE: tests/test_risk.py is stale, see below
ruff check helm tests scripts
mypy helm
```

The Makefile's `make test`, `make lint`, `make dry-run-*`, `make backtest`, `make dashboard`, `make configure`, `make verify` mostly point at code that doesn't exist anymore. Use the raw commands above instead.

## Stale code to be aware of

- **`tests/test_risk.py`** asserts on `RiskLimits` fields that no longer exist (`max_daily_loss_pct`, `sleeve_b_cap_pct`, etc. — those were the old PRD limits). Current `RiskLimits` lives in `helm/config.py` with different fields (`max_open_positions`, `max_position_inr`, `daily_loss_kill_inr`, `per_symbol_cooldown_min`, `max_signals_per_symbol_per_day`). The other tests are `@pytest.mark.skip`. If asked to add tests, either rewrite this file against the current `RiskLimits` + `risk.evaluate` or delete it.
- **`helm/orchestrator/cli.py`** still has `configure`/`verify`/`status` stubs that print "TODO Phase 3" — not wired to anything real. The kill-switch import path `helm.orchestrator.kill_switch` exists but the orchestrator/cli is not part of the live bot loop.
- **`helm/orchestrator/{audit,notify,kill_switch}.py`** exist but are not on the cron path; only `risk.py` is actively called (from `paper_execute.py` and `decide_signals.py`). `helm.data.store.insert_audit` is the function actually used for audit writes, not `helm.orchestrator.audit`.
- **`helm/brokers/zerodha.py`** exists but no script imports it; current code goes directly to `kiteconnect.KiteConnect` (see `dashboard/app.py`, `scripts/dry_run_zerodha.py`, `scripts/kite_login.py`).
- **`README.md`** describes Sleeve A/B, IBKR, `backtrader`, and four dry-run gates. Out of scope.

## Conventions worth preserving

- IST everywhere for trading-day boundaries; never use `now()` raw in SQL "today" filters — use the `date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'` pattern.
- Decimals (`from decimal import Decimal`) for prices/PnL — not floats. The DB columns are `NUMERIC(12,2)` and psycopg returns `Decimal`.
- `insert_audit(actor, event, detail_dict)` for any noteworthy state change. Cron scripts log a per-run summary to `audit` so the dashboard "recent audit" panel shows what happened.
- Cron scripts must no-op silently outside market hours / on weekends — cron fires them off-hours and we don't want log spam.
- `consumed` flag on `signals` is the dedupe boundary between strategies and the decider; `paper_execute` flips it inside the same transaction as the `decisions` insert.

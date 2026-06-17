# Helm — Product Requirements Document (Master)

| | |
|---|---|
| **Product** | Helm — an LLM-gated, self-improving, multi-agent trading platform |
| **Status** | Living document. Sections tagged ✅ Built · 🚧 In development · 📋 Planned |
| **Owner** | TechInject (single operator / judge) |
| **Author** | Engineering delegate (Claude) |
| **Last updated** | 2026-05-23 |
| **Deployment** | `https://helm.techinject.co.in` — single Linux VPS, PM2 + nginx + Let's Encrypt |
| **Scale** | ~13,300 LOC Python · 20-table PostgreSQL schema · 8-page dashboard |
| **Source of truth for config** | `helm/config.py` (hardcoded by design) |

> This PRD documents the **whole product as it actually exists in the code today**, plus what is under active development and what is planned. Where the original README/PRD (`Helm_PRD_v1.3.docx`) describes scrapped scope (passive sleeve, IBKR, `backtrader`, dry-run gates G1–G4), that scope is explicitly **out** and is not represented here. Every requirement below is grounded in shipped code unless tagged 🚧 or 📋.

---

## 1. Executive summary

Helm started as a single-user intraday paper-trading bot for the top-5 NSE large caps. It has grown into a three-layer **agentic operations platform** where large language models are wired into a real backend — not as a chatbot bolted on the side, but as decision-makers, graders, and engineers inside a live, audited, cron-driven loop:

1. **Layer 1 — The LLM-gated trading loop (✅ live).** Mechanical strategies propose trades; Claude judges each one TAKE/SKIP with reasoning; a hard risk gate has the final word; a second LLM call grades every closed decision in plain English.
2. **Layer 2 — The self-improvement loop (✅ built).** Three cooperating agents (Product Manager → Engineer → Tester) turn the graded mistakes into shipped, verified code changes — the system edits and tests itself, with a human as the only push gate.
3. **Layer 3 — The competition league (✅ built, awaiting go-live).** Five different AI CLI backends (Claude, Gemini, Qwen, Codex, opencode) each trade an isolated ₹50k paper wallet with their own strategies, so the operator can watch equity *and* reasoning quality compete head-to-head.

Everything runs on cron, no-ops silently outside market hours, persists every state change to an audit log, and is observable through one Streamlit dashboard. The POC runs at **near-zero marginal cost** by routing LLM calls through subscription/free CLIs behind a single swappable transport abstraction.

The reusable asset is the **pattern**, not the trading code: *deterministic producers propose → an LLM judges → a hard validator guards → a second LLM grades → agents improve the loop.* That scaffold drops into lead qualification, support-ticket triage, document review, content moderation — any high-volume stream of decisions that needs both pre-action judgment and post-action explanation.

---

## 2. Problem statement

A pure rule-based automation is blind: a rule fires, the system acts, nobody asks whether the action made sense until it has already cost something. Three compounding pains follow:

- **No judgment at the point of action.** Rules can't weigh "breakout into resistance" or "signal arriving minutes before square-off." A human would skip; the bot can't.
- **No accountable explanation after the fact.** Once a decision is logged, the operator has no easy way to audit *why* it was made, or to learn from it — especially a non-engineer reading the day's tape.
- **No path from lesson to fix.** Even when mistakes are spotted, turning them into shipped improvements is manual, slow, and never happens consistently.

Helm exists to keep the mechanical layer (fast, deterministic, cheap) while adding an LLM judge in front of every action, an LLM grader behind every outcome, and an agent team that closes the loop from lesson to deployed change.

---

## 3. Goals & non-goals

### Goals
- Gate every automated action with LLM judgment **without** giving up the speed/determinism of the rule layer.
- Make every decision (including every SKIP and every risk-gate block) **auditable and explainable in plain English**.
- Convert graded mistakes into **shipped, verified changes** with minimal human involvement (human = push gate only).
- Run the whole thing at **near-zero marginal cost** during the POC, behind interfaces that swap to paid/production transports with one env var.
- Be **observable end-to-end** from a single dashboard.

### Non-goals
- ❌ Real-money trading. Helm is paper-trading only; this is a research/POC system.
- ❌ Passive investing, IBKR, SIPs, `backtrader`, dry-run gates G1–G4 — original-PRD scope, scrapped.
- ❌ Multi-symbol portfolio optimization or cross-signal reasoning. One signal → one decision.
- ❌ Going short. Long-only v1 across all layers.
- ❌ A YAML/JSON config file, a connection pool, an async layer, or a message queue. Deliberately a small, single-user, synchronous Postgres app.
- ❌ Switching market data to Kite quotes (the account lacks the market-data add-on; those calls 403 — see §11).

---

## 4. Users & personas

Helm is **single-user by design**. One person wears three hats:

| Hat | What they do | Surface |
|---|---|---|
| **Operator** | Starts/stops the loop, edits risk caps live, refreshes the broker token | Dashboard Settings page, `settings` table, CLI scripts |
| **Judge** | Watches the competition league — equity *and* reasoning quality across the 5 agents | Competition League dashboard page |
| **Owner** | Reviews the agents' commits and pushes to the remote (the only human gate in the self-improvement loop) | `git`, `RELEASES.md`, Self-Improvement Loop page |

No multi-tenant, no auth roles, no RBAC. The dashboard sits behind nginx + an operator login.

---

## 5. System overview

Three layers share one PostgreSQL database (`dbname=helm`, local Unix socket, autocommit, `dict_row`). Everything is driven by cron; nothing is a long-running worker.

```
                         ┌─────────────────────────────┐
                         │   Streamlit dashboard (8 pp) │  read-only over Postgres
                         └──────────────┬──────────────┘
                                        │
        Layer 1 — Trading loop          │          Layer 3 — Competition league
        poll → scan → DECIDE(LLM)       │          5 CLI agents × ₹50k wallets
        → RISK GATE → trade → manage    │          freestyle OPEN/CLOSE/HOLD
        → RETRO(LLM)                    │          → shared risk gate → isolated books
                   \                    │                    /
                    \         ┌─────────▼─────────┐         /
                     ─────────►   PostgreSQL 20    ◄────────
                              │   tables (one DB)  │
                              └─────────▲─────────┘
                                        │
        Layer 2 — Self-improvement loop │
        retros → proposals → PM(LLM) → Engineer(LLM, commits code)
        → Tester (verifies / reverts) → human pushes
```

---

## 6. Detailed requirements

### 6.1 Market-data ingestion ✅
- **Trigger:** `scripts/poll_market.py`, every minute 03:45–10:00 UTC (≈ NSE session in IST), Mon–Fri only; no-op otherwise.
- **Source:** `yfinance` (`yf.Ticker(f"{SYM}.NS").fast_info.last_price`). Kite quote APIs are not available on this account (§11).
- **Behavior:** pull last price for each `WATCHLIST` symbol → insert `ticks` → `roll_minute_candles()` upserts `candles_1m` (1-minute OHLC).
- **Competition extension (🚧 go-live):** `scripts/poll_competition.py` additionally polls the *union* of all competitors' weekly-mandate symbols that fall outside `WATCHLIST`, so the league universe is data-covered without double-polling.

### 6.2 Strategy / signal layer ✅
- **Interface:** `helm/strategies/base.py` — `Strategy` ABC, one pure method `scan(symbol, candles) -> Signal | None`. **No DB access from strategies.**
- **Active strategies:** Opening-Range Breakout (`orb.py`), VWAP reclaim (`vwap.py`), gap-fade (`gap_fade.py`); registered in `helm/strategies/__init__.ACTIVE`. Variants (e.g. `OpeningRangeBreakout(or_minutes=…)`) can be registered without new code via the self-improvement loop's `add_strategy_variant` mutator.
- **Trigger discipline:** strategies fire **only on the breakout/trigger bar** (e.g. ORB checks the prior bar didn't already break out) so re-running the scan never re-emits the same idea.
- **Dedupe boundary:** the `consumed` flag on `signals` is the contract between strategies and the decider.

### 6.3 LLM decision gate (the decider) ✅
- **Trigger:** inline inside `scripts/scan_signals.py` (synchronous, ~5–10s end-to-end), with `scripts/decide_signals.py` as a 2-minute catch-up safety net.
- **Behavior:** for each unconsumed signal, build a context blob — recent ~30 bars, today's open positions, current risk state, today's prior decisions on the same symbol — and ask Claude (default `claude-sonnet-4-6`, override via `DECIDER_MODEL`) for strict JSON `{verdict: TAKE|SKIP, confidence, reasoning}`.
- **Engineering choices (trust signals):**
  - **Prompt caching** on the system prompt (`cache_control: ephemeral`) in API mode.
  - **Tolerant JSON parser** strips ```` ``` ```` fences the model adds despite instructions.
  - **Every signal yields exactly one `decisions` row** (TAKE or SKIP, including risk-gate blocks), so every SKIP is auditable.
  - On API error / malformed JSON: log to `audit`, leave the signal unconsumed for retry — never silently drop.
  - **Staleness guard:** the catch-up cron auto-SKIPs anything older than `STALE_THRESHOLD_MINUTES=20` without burning an LLM call.
  - Trading window guarded by `_within_window`; `--force-window` bypasses for manual runs.

### 6.4 Risk gate & execution ✅
- **Single chokepoint:** `paper_execute.execute_signal` is the only path that opens a trade, and it calls `helm/orchestrator/risk.evaluate` before every open. No code path bypasses it.
- **Hard limits (`RiskLimits` in `helm/config.py`):** `max_open_positions`, `max_position_inr`, `daily_loss_kill_inr`, `per_symbol_cooldown_min`, `max_signals_per_symbol_per_day`, plus available wallet cash.
- **Dynamic sizing:** per-trade notional = `min(dynamic_position_cap(realised_pnl, base_cap), wallet.available) // entry_price`. The cap starts at `max_position_inr` (₹15k) and adds ₹1.5k per ₹5k of realised profit, capped at ₹25k — winners compound, losers never shrink the cap below baseline.
- **Live-editable caps:** risk limits are adjustable from the dashboard via the `settings` table with no PM2 restart.
- **Atomicity:** the `consumed` flag flips inside the same transaction as the `decisions` insert, so the decider cron can never double-act on a signal.

### 6.5 Position management ✅
- **Trigger:** `scripts/manage_positions.py`, every minute.
- **Behavior:** walk OPEN `paper_trades`; close on stop/target hit (priced via `yfinance`), or EOD square-off at `SQUARE_OFF_AT` (15:15 IST). Uses a Zerodha-MIS charge model for realistic net P&L. `Decimal` throughout (DB columns are `NUMERIC(12,2)`).

### 6.6 Retrospective & learning pipeline ✅
- **Retrospectives (`helm/retro.py`, `scripts/retro_trades.py`):** after a trade closes — or after a SKIP can be judged against the rest of the day's tape — a second Claude call writes a plain-English retrospective: why we acted, what price did, a five-way verdict (`GOOD_CALL | BAD_CALL | LUCKY | UNLUCKY | MIXED`), 1–3 concrete learnings, and three quality scores (signal / decision / execution).
  - **SKIP grading via counterfactual replay:** a replay engine walks the day's candles forward, treats the proposed entry as a touch-fill, and reports what the trade *would* have done.
  - **Plain-English mandate:** the retro system prompt carries a banned-jargon list that forces the model to translate "ORB", "VWAP reclaim", "R:R" into language a non-trader can read.
  - **Idempotency:** unique indexes make retros idempotent on `trade_id` (trade reviews) and `decision_id` (skip reviews); a Postgres advisory lock prevents an overshooting cron run from racing the next firing and burning LLM quota.
- **Improvement proposals:** every retrospective emits 0–3 concrete change ideas, each persisted as an `improvement_proposals` row with a category ∈ `strategy | decider_prompt | risk | sizing | execution | data | meta`. `scripts/proposals_digest.py` aggregates them across retros, ranks by recurrence × confidence, and runs a status workflow (`open → accepted → applied`, or `rejected`/`superseded`). This is the queue the self-improvement loop consumes.

### 6.7 Autonomous self-improvement loop ✅
Three cooperating agents turn `improvement_proposals` into shipped, verified changes. Charters in `AGENTS/`. All three use `helm.llm.complete_json`; all writes are audited.

| Agent | Cadence | Role | Output |
|---|---|---|---|
| **Product Manager** (`helm/agents/pm.py`, `scripts/pm_review.py`) | Mondays 06:00 IST | Triage the proposal queue against a goal brief (equity vs goal, 7-day P&L, win rate, expectancy, queue depth). Recurrence beats novelty; ship the cheap reversible thing first; one change per task; no churn while a release is unverified. | 0–3 `agent_tasks` (typed), proposals moved `open→accepted`, one `agent_runs` trace |
| **Engineer** (`helm/agents/engineer.py`, `scripts/engineer_run.py`) | Every 30 min, weekdays | Convert one task into a committed change on `main` via a **closed set of typed mutators** — never free-form coding. Out-of-surface tasks → `needs_human`. | Edited code, lint+tests pass (hard gate), one local `git commit`, a `releases` row, `RELEASES.md` entry |
| **Tester** (`helm/agents/tester.py`, `scripts/tester_run.py`) | Reactive, every 5 min | Verify each unverified release on the live deployment (static gates → pipeline smoke → dashboard reachability → PM2 health → pre-existing-feature spot check → 24h P&L sanity). On fail: `git revert`, file a `bug_fix` task, keep `main` always-green. | `releases.status = verified` or `reverted`, `agent_runs` stage trace |

- **Engineer mutator surface (the safety boundary):** `prompt_tweak`, `param_change`, `add_filter`, `setting_override`, `add_strategy_variant`, `bug_fix` — each with a strict `spec` schema validated before apply. The mutator never touches schema files, never adds dependencies, never edits cron, never edits the agents layer itself. Fuzzy anchor matches fail loudly rather than guessing.
- **Concurrency safety:** `claim_next_task` uses `FOR UPDATE SKIP LOCKED`; the tester locks by advisory key; backpressure halts the PM/Engineer when ≥2 releases are unverified.
- **Human gate:** the Engineer **never pushes**. Commits land locally on `main`; the human reviews and pushes. The dashboard surfaces unpushed commits.
- **Kill switch:** `INSERT INTO settings (key,value,updated_by) VALUES ('autonomy_paused','true','manual')`. All three agents check the flag at start. Two consecutive reverts auto-pause the loop.

### 6.8 Multi-agent competition league ✅ (built, 🚧 awaiting go-live)
Built across phases J0–J5 on branch `feat/competition-league` (committed, **not pushed, not on cron** — going live is a human decision because each cycle spends backend quota). The incumbent `house-claude` pipeline is untouched.

- **Cohort (5):** `claude` (incumbent, subscription) + `gemini` + `qwen` + `codex` + `opencode`, each a different CLI backend.
- **Autonomy = freestyle:** each agent emits its own OPEN/CLOSE/HOLD trade-intent JSON from a market snapshot (its own symbols, its own logic), validated by the **same** risk gate and routed through the **same** `paper_execute` chokepoint into its **own** isolated ₹50k wallet.
- **Weekly mandates (`helm/competition/mandate.py`):** each agent picks ≤15 symbols from a curated 45-symbol `TRADABLE_UNIVERSE` plus a free-form strategy config and rationale; one idempotent row per `(competitor_id, week_start=Monday)`.
- **Per-backend quota subsystem (`helm/competition/quota.py`):** a rolling per-backend call window in `backend_quota_state`; on exhaustion → log + pause-until-reset + auto-resume; a paused backend is skipped without spending anything. One shared, traced chokepoint `call_backend()` is used by both the runner and the planner.
- **Isolation guarantees:** `risk.evaluate` takes an optional `competitor_id` (None ⇒ house book); every `signals`/`decisions`/`paper_trades` row carries `competitor_id`; the house wallet filters to house rows so league trades never leak into the incumbent's equity or sizing.
- **Cadence (when live):** mandates weekly Monday; poll per-minute; decide every 5 minutes; ₹50k each.
- **Observability (`scripts/league_status.py` + dashboard page 8):** equity-ranked leaderboard on a common ₹50k basis, this-week mandates, and a **"how it thinks"** view parsing `agent_invocations` into actions/commentary with raw prompt+response.
- **Backend auth status:** `claude` + `opencode` usable now ($0); `gemini` resolved via a free GCP API key; `qwen` + `codex` need a one-time human OAuth login before they can compete (see `docs/competition-backends-status.md`).

### 6.9 Dashboard ✅
- **`helm/dashboard/app.py`** + 8 pages, **read-only over Postgres** (plus a Kite `profile()`/`margins()` call). Runs under PM2 (`ecosystem.config.js`) on `127.0.0.1:8501` behind nginx + Let's Encrypt at `helm.techinject.co.in`.
- **Pages:** How It Works · Summary · Activity Log · Charts · Settings · Retrospectives · Self-Improvement Loop · Competition League.

### 6.10 LLM transport abstraction ✅
- `helm/llm.py` hides the model transport behind one function with interchangeable backends, selected by env var:
  - `LLM_MODE=cli` (POC default) → Claude Code subscription via `claude -p` (and the J0 `CLI_ADAPTERS` registry for gemini/qwen/codex/opencode).
  - `LLM_MODE=api` → Anthropic SDK direct (per-token, prompt-cached).
- The rest of the codebase never branches on transport — callers use `complete_json(...)` / `decide(...)`. Backend precedence: `backend` arg → `LLM_BACKEND` env → default `claude`. The existing `claude` CLI path is preserved byte-for-byte.

---

## 7. Data model

PostgreSQL, schema in `helm/data/schema.sql`. All timestamps `timestamptz`; "today" boundaries use `date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'`. Money is `NUMERIC(12,2)` → `Decimal` in Python.

| Group | Tables |
|---|---|
| Market data | `ticks`, `candles_1m` |
| Trading core | `signals`, `decisions`, `paper_trades`, `daily_state` |
| Ops | `audit`, `settings` |
| Learning | `trade_retrospectives`, `improvement_proposals` |
| Self-improvement loop | `agent_tasks`, `releases`, `agent_runs`, `metrics_snapshots` |
| Competition league | `competitors`, `competitor_wallets`, `competitor_mandates`, `agent_invocations`, `backend_quota_state` |
| Agent-aware config (🚧) | `competitor_config_versions` |

Connection helper: `helm.data.store.conn()` (autocommit, `dict_row`). `init_schema()` is idempotent. The competition migration (`scripts/migrate_competition.py`) is an idempotent backfill that seeds `house-claude` + its wallet and stamps existing rows.

---

## 8. Non-functional requirements

- **Cost (POC):** near-zero marginal cost — LLM calls route through the Claude Code subscription and free CLI backends; `ANTHROPIC_API_KEY` is never set in POC mode. Workarounds are tracked in `WORKAROUNDS.md` with explicit removal triggers (swap to `LLM_MODE=api` when paper-trading shows positive expectancy or rate limits start blocking).
- **Idempotency & dedupe:** `consumed` flag on signals; unique indexes on retros (`trade_id` / `decision_id`); idempotent schema + migration; `ON CONFLICT DO NOTHING` upserts; advisory locks on the retro and tester crons; `FOR UPDATE SKIP LOCKED` task claiming.
- **Observability:** `insert_audit(actor, event, detail)` on every noteworthy state change; per-run summaries to `audit`; `agent_runs` / `agent_invocations` ledgers; the dashboard renders the whole DB back to the operator.
- **Resilience / no spam:** every cron no-ops silently outside market hours and on weekends; LLM failures leave work for retry rather than dropping it.
- **Security boundary:** no secrets in code (`.env` for keys); the Engineer agent's closed mutator surface cannot touch schema, deps, cron, or itself; the human is the only push gate; dashboard behind nginx + LE + operator login.
- **Determinism where it matters:** the Tester's happy path is deterministic (LLM only summarizes the verdict); the risk gate is pure code, always the final word.
- **Style/quality gates:** `ruff` (line-length 100, py311), `pytest`, `mypy helm` (tracked baseline; agents must not introduce new errors).

---

## 9. Runtime & deployment

- **Host:** single Linux VPS. **Process manager:** PM2 (dashboard). **Edge:** nginx + Let's Encrypt at `helm.techinject.co.in` (port 8501 upstream).
- **Cron topology** (`scripts/run_in_venv.sh` activates `.venv`):
  - *Trading loop (live):* `poll_market` (1m), `scan_signals` (1m, scan+inline-decide), `decide_signals` (2m catch-up), `manage_positions` (1m).
  - *Learning:* retro/metrics jobs (advisory-locked).
  - *Self-improvement (built):* `pm_review` (Mon 06:00 IST), `engineer_run` (30m weekdays), `tester_run` (5m reactive).
  - *Competition (built, not yet scheduled):* `plan_mandates` (weekly), `poll_competition` (1m), `run_competitors` (5m).
- **Broker token:** `KITE_ACCESS_TOKEN` regenerated daily via `scripts/kite_login.py` (Kite tokens expire 06:00 IST; logging into Kite web/app invalidates the API token mid-day).

---

## 10. Roadmap — Built · In development · Planned

### ✅ Built & live (production)
- Layer 1 trading loop end-to-end (poll → scan → decide → risk gate → execute → manage).
- Retrospective + improvement-proposal learning pipeline.
- Self-improvement loop (PM / Engineer / Tester) with typed mutator surface and revert safety.
- 8-page Streamlit dashboard.
- LLM transport abstraction (cli/api, multi-backend registry).

### ✅ Built · 🚧 awaiting human go-live
- **Competition league (J0–J5)** on `feat/competition-league`: backend registry + smoke harness, isolated wallets + schema, freestyle runner, weekly mandates + dynamic poller, per-backend quota subsystem, leaderboard + dashboard page. Go-live = log in the remaining backends, seed competitors, plan mandates, add the three crons, push the branch.

### 🚧 In development
- **Agent-aware self-improvement loop:** PM → Engineer → Tester generalized to operate per-agent — for *house* (code mutators) and for *freestyle* competitors (persona / config mutators), with a new `competitor_config_versions` table tracking versioned persona/config changes and a consolidated weekly cron. (Modified `helm/agents/*`, `helm/competition/mandate.py`, `helm/config.py`, `helm/data/schema.sql`, `helm/llm.py`, `helm/retro.py`; new `tests/test_agent_aware_loop.py`.)

### 📋 Planned / deferred
- **Multi-agent debate decider** (`docs/prd/multi-agent-debate-decider.md`): replace the single-shot TAKE/SKIP with a bull-vs-skeptic (or full bull/bear/risk-manager) deliberation, flag-gated and A/B-able. Tier 1 (self-critique, prompt-only) ships as one reversible PR; Tiers 2/3 are scoped engineering tasks once Tier 1 shows lift.
- **Backend authentication completion:** one-time human OAuth for `qwen` and `codex` to bring the full 5-agent cohort online.
- **Production transport swap:** move the decider off the subscription CLI to `LLM_MODE=api` once expectancy is proven or rate limits bite (per `WORKAROUNDS.md`).

---

## 11. Known constraints & workarounds

- **No Kite market-data add-on.** `kite.ltp()/quote()/ohlc()/historical_data()` all 403 (PermissionException) on this account; market data comes from `yfinance` (`{SYM}.NS`). Order placement, `profile()`, and `margins()` do work. Do not "fix" by switching to Kite quotes.
- **Subscription-CLI LLM transport** is an explicit POC cost workaround (tracked in `WORKAROUNDS.md`): no cache-control knob, subscription rate-limit risk under automated load, ToS gray area. Mitigated by the tolerant parser and the one-env-var swap to API mode.
- **Kite single-session tokens:** logging into Kite web/app kills the API access token; a successful morning cron does not guarantee the token survives mid-day.
- **`mypy helm` baseline:** a pre-existing error baseline exists (dict_row typing cascade, anthropic union-attr); the rule is "introduce zero new errors," not "zero errors."
- **Stale code (do not reintroduce):** `tests/test_risk.py` asserts on old PRD `RiskLimits`; `helm/orchestrator/{cli,audit,notify,kill_switch}.py` and `helm/brokers/zerodha.py` exist but are off the live path; `README.md` describes scrapped Sleeve A/B + IBKR + `backtrader` scope.

---

## 12. Success metrics

- **Decision quality:** win rate and expectancy per TAKE; BAD_CALL retro rate trending down.
- **Loop health:** 100% of `main` HEAD commits reach `verified` or `reverted` (never silent `deployed`); median deploy→verdict < 10 min; ≤5% of agent tasks bounce as spec-validation failures.
- **Coverage:** every signal yields exactly one auditable decision; every closed trade and judged SKIP yields exactly one retrospective.
- **Cost:** $0 marginal LLM spend through the POC.
- **League (when live):** equity *and* reasoning-quality comparison across all authenticated backends, on a common ₹50k basis.

---

## 13. Glossary

- **House / `house-claude`** — the incumbent single-portfolio trading loop (Layer 1), modeled as competitor `house-claude` so the league can rank it alongside the others.
- **Freestyle** — a competitor agent that emits its own OPEN/CLOSE/HOLD intents from a snapshot, rather than running fixed strategies.
- **Decider** — the LLM TAKE/SKIP gate in front of execution.
- **Retro** — the LLM plain-English grade of a closed decision.
- **Mutator** — one of the Engineer agent's typed, schema-validated code-edit operations.
- **Mandate** — a competitor's weekly declaration of its symbol universe + strategy config.
- **Risk gate** — the pure-code hard validator (`risk.evaluate`) that has the final say before any trade opens.

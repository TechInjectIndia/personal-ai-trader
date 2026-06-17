# Build Brief — Multi-Agent Trading Competition (Coordinator)

You are the **Coordinator** for an autonomous, multi-agent build running detached in tmux on a VPS.
You have NO prior conversation context — this file is your single source of truth.

## Hard rules (read first, never violate)
1. **$0 spend.** Run on the Claude subscription only. NEVER set or export `ANTHROPIC_API_KEY`. Do not switch any code to `LLM_MODE=api`.
2. **Commit after every completed job** on branch `feat/competition-league` (create it from `main` first). Small, frequent commits — a quota cutoff must never lose work. Commit message footer:
   `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
3. **NEVER `git push`.** Local commits only (house rule: the human pushes).
4. **THIS RUN = J0 + J1 ONLY.** After both are green and committed, write the checkpoint status file (below) and STOP. Do NOT start J2–J5.
5. If you hit the rolling-window quota and stall: ensure latest work is committed, append a `## QUOTA HALT` note to `docs/competition-build-status.md` describing exactly where you stopped, and stop cleanly.
6. Obey `CLAUDE.md` conventions: Postgres via `helm.data.store.conn()` (autocommit, dict_row, dsn `dbname=helm`); `Decimal` for money; IST idiom `date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'`; `insert_audit(...)` for state changes; NO new YAML/JSON config file (config lives in `helm/config.py`); NO connection pool / async layer. Ruff line-length 100, py311, `mypy helm` should pass.

## Mission context (the feature being built)
A paper-trading **competition/league**: 5 AI agents, each a different CLI backend, trade autonomously with isolated ₹50k wallets; the human is the judge watching equity AND reasoning quality. Locked product decisions:
- **Cohort (5):** `claude` (incumbent, subscription) + `gemini` (free) + `qwen` (free) + `codex` + `opencode`.
- **Autonomy = freestyle:** each agent emits its own trade-intent action list (OPEN/CLOSE/HOLD JSON) from a market snapshot — its own stocks, its own logic — validated against the risk gate, routed through the existing `paper_execute` chokepoint into its own wallet.
- **Quota management (later phase):** keep free-tier agents inside free quotas; on exhaustion log + pause-until-reset + auto-resume.
- **Universe:** anything tradable on the Zerodha account (NSE equities), priced via `yfinance` (`{SYM}.NS`) — Kite quotes 403. Each agent declares ≤~15 symbols in its weekly mandate; a dynamic poller polls the union.
- **Capital/cadence:** ₹50k each, decide every 5 min.
- The "Multi-Agent Debate Decider" PRD (`docs/prd/multi-agent-debate-decider.md`) is **deferred — out of scope**.

Full phase map (for context; only J0+J1 this run):
`J0 backend registry+smoke` · `J1 schema+wallets` → then `J2 freestyle runner` · `J3 weekly mandate + dynamic poller` · `J4 quota subsystem` · `J5 dashboard pages` → integrate → tester.

## How to orchestrate (the agent structure)
Use the `Agent` tool. J0 and J1 are independent (different files) → run them as **two parallel workers in isolated git worktrees**, then a tester, then you merge + integration-test + score.

1. **Worker-A → J0** — `Agent(subagent_type: general-purpose, isolation: "worktree")`. Touches `helm/llm.py` + adds a smoke-test script. (If unsure about a CLI's headless flags, it may use a `claude-code-guide` agent.)
2. **Worker-B → J1** — `Agent(subagent_type: general-purpose, isolation: "worktree")`. Touches `helm/data/schema.sql` + `helm/data/store.py` + a migration/backfill.
3. Spawn A and B in one message so they run concurrently.
4. **Tester** — after merging both: `Agent(subagent_type: general-purpose)` to run `ruff check helm tests scripts`, `mypy helm`, `pytest -q` (note: `tests/test_risk.py` is stale per CLAUDE.md — that's acceptable), `python -c "from helm.data.store import init_schema; init_schema()"`, and review each worker's diff for correctness + convention adherence. Score each job 0–5 on (correctness, convention-fit, test-pass) in the status file.
5. **You merge** each worktree branch into `feat/competition-league`, resolve conflicts, fix integration breaks, re-run the tester until green, commit.

## J0 — Backend registry + smoke-test
Goal: generalize `helm/llm.py`'s single `_complete_cli` into a **backend registry** so the system can call any of {claude, gemini, qwen, codex, opencode} headlessly and get back parsed JSON, AND record which backends are actually usable right now.

- In `helm/llm.py`, refactor `_complete_cli` into a per-backend adapter map. Each adapter builds the correct headless command and parses stdout to JSON. Keep the existing `claude` path working byte-for-byte (it is proven — do not break it). Sketch of known headless invocations (verify flags with `--help`; adapt, don't trust blindly):
  - claude: `claude -p <user> --system-prompt <sys> --output-format json --model <m> --tools "" --dangerously-skip-permissions --json-schema <schema>` (already implemented).
  - gemini: `gemini -p "<prompt>"` (Google login). codex: `codex exec "<prompt>"` (ChatGPT/login). qwen: `qwen -p "<prompt>"`. opencode: `opencode run "<prompt>"`.
  - Backend selected by a new optional `backend` arg / `LLM_BACKEND` env, defaulting to the current `claude` cli path. Do NOT change the default behavior of `decide()`/`complete_json()` for existing callers.
- Attempt to install the four CLIs (`npm i -g @google/gemini-cli @qwen-code/qwen-code @openai/codex opencode-ai` or their correct package names — verify; some may differ). Record success/failure.
- For each backend, attempt ONE headless smoke call asking for a tiny JSON object. **Time out fast (≤30s).** Classify each as: `OK` (valid JSON returned) / `NEEDS-AUTH` (CLI runs but demands interactive login) / `NOT-INSTALLED` / `BAD-OUTPUT`. **Do NOT attempt interactive OAuth — an unattended agent cannot complete it; mark NEEDS-AUTH and move on.**
- Write `docs/competition-backends-status.md` with the result matrix + the exact login command the human must run for each NEEDS-AUTH backend.
- Add `scripts/smoke_backends.py` (re-runnable later by the human after they log in).
- Do NOT write Anthropic API keys anywhere.

## J1 — Schema migration + isolated wallets
Goal: add the competitor dimension and per-agent capital, idempotently, without breaking the running single-portfolio bot.

- Edit `helm/data/schema.sql` (idempotent `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`):
  - New `competitors` (id text PK, name, backend, model, persona TEXT, autonomy_level, status, created_at timestamptz).
  - New `competitor_wallets` (competitor_id FK, initial_capital_inr NUMERIC(12,2), available_inr, realized_pnl_inr, updated_at).
  - New `competitor_mandates` (id, competitor_id, week_start DATE, universe JSONB, strategy_config JSONB, rationale TEXT, raw JSONB, created_at).
  - New `agent_invocations` (id, competitor_id, ts, backend, model, prompt TEXT, raw_output TEXT, latency_ms INT, ok BOOL, error TEXT) — the per-call "how it thinks" + cost/quota ledger.
  - New `backend_quota_state` (backend PK, window_start timestamptz, calls_used INT, paused_until timestamptz).
  - Add nullable `competitor_id TEXT` to `signals`, `decisions`, `paper_trades`, `daily_state`, `metrics_snapshots` (ADD COLUMN IF NOT EXISTS). Keep them nullable so existing rows/code keep working.
- `init_schema()` must remain idempotent and succeed (`python -c "from helm.data.store import init_schema; init_schema()"`).
- Add a backfill: insert one competitor row `id='house-claude'` (the existing portfolio) + its wallet seeded from `WalletConfig.initial_capital_inr`, and stamp existing `paper_trades`/`decisions`/`signals` with `competitor_id='house-claude'` where NULL. Make it re-runnable (idempotent upsert). Put it in `scripts/migrate_competition.py` or extend `init_schema`.
- Do NOT change `helm/config.py` risk/wallet values. Do NOT alter the live cron scripts' behavior — additive only.

## Checkpoint (end of THIS RUN)
When J0 + J1 are merged, green, and committed on `feat/competition-league`:
1. Write `docs/competition-build-status.md` containing: which jobs are done, the backend status matrix (copy from backends-status), the tester scores, any deviations from this brief, and an explicit `## NEXT: J2–J5 (awaiting human go)` section.
2. Print a final summary to stdout.
3. **STOP.** Do not begin J2.

# Competition-League Build — Status (Checkpoint after J0 + J1)

**Branch:** `feat/competition-league` (created from `main` @ `614f5fd`)
**This run scope:** J0 + J1 only. **STOPPED at the checkpoint as instructed — J2–J5 NOT started.**
**Generated:** 2026-05-20, on the VPS, by the Coordinator agent.
**Spend:** $0 — Claude subscription only. `ANTHROPIC_API_KEY` was never set/exported; no code switched to `LLM_MODE=api`. Nothing was pushed (local commits only).

## Commits on this branch (top of branch first)

| Commit | What |
|---|---|
| `f6fb45e` | Merge J1 (competition schema + wallets) into `feat/competition-league` |
| `9eaf727` | **J0** — backend registry + headless smoke-test for competition CLIs |
| `5970015` | **J1** — competition schema + isolated wallets + house-claude backfill |
| `614f5fd` | (base — `main`) |

Both jobs share parent `614f5fd` and touch **disjoint files**, so the merge was conflict-free.

## Jobs done

### J0 — Backend registry + smoke-test ✅
- `helm/llm.py`: `_complete_cli` refactored into a `CLI_ADAPTERS` registry (claude / gemini / qwen / codex / opencode). Precedence: `backend` arg → `LLM_BACKEND` env → default `claude`. The existing `claude` command construction is preserved **byte-for-byte**; existing callers and the `api` mode path are unchanged (verified against `614f5fd`).
- `scripts/smoke_backends.py`: re-runnable smoke harness, fast **30s** per-call timeout, never makes a paid call.
- `docs/competition-backends-status.md`: result matrix + per-backend human login commands.

### J1 — Schema migration + isolated wallets ✅
- `helm/data/schema.sql` (additive, idempotent): new tables `competitors`, `competitor_wallets` (one wallet per competitor), `competitor_mandates` (+UNIQUE on competitor_id, week_start), `agent_invocations`, `backend_quota_state`; plus nullable `competitor_id TEXT` (no FK, no default) on `signals`, `decisions`, `paper_trades`, `daily_state`, `metrics_snapshots` — so the live single-portfolio bot keeps working untouched.
- `scripts/migrate_competition.py`: idempotent backfill — seeds competitor `house-claude` + its ₹50,000 wallet (from `WalletConfig().initial_capital_inr`, `Decimal`), stamps existing `signals`/`decisions`/`paper_trades` rows where `competitor_id IS NULL`, and writes one `insert_audit('migrate_competition','backfill', …)`. Re-runs are no-ops (`ON CONFLICT DO NOTHING`; live wallet P&L preserved).
- `init_schema()` remains idempotent and succeeds.

## Backend status matrix (copied from `docs/competition-backends-status.md`)

| backend  | package | install result | smoke result | classification | notes |
|----------|---------|----------------|--------------|----------------|-------|
| claude   | (pre-installed) | already on PATH (`~/.local/bin/claude`) | `{"ok": true}` ~6–10s | **OK** | Subscription auth. Proven path, unchanged. |
| opencode | `opencode-ai@1.15.5` | `~/.npm-global/bin/opencode` | `{"ok": true}` ~5–9s | **OK** | Bundled free gateway model `opencode/big-pickle`; works with no stored creds ($0). |
| gemini   | `@google/gemini-cli@0.42.0` | `~/.npm-global/bin/gemini` | exit 41, demands auth | **NEEDS-AUTH** | Set free `GEMINI_API_KEY` (AI Studio) **or** interactive Google login. |
| qwen     | `@qwen-code/qwen-code@0.15.11` | `~/.npm-global/bin/qwen` | exit 1, "No auth type selected" | **NEEDS-AUTH** | `qwen auth qwen-oauth` (free Qwen-OAuth). |
| codex    | `@openai/codex@0.132.0` | `~/.npm-global/bin/codex` | exit 1, `401 Unauthorized` | **NEEDS-AUTH** | `codex login` (ChatGPT). Adapter passes `--skip-git-repo-check --sandbox read-only`. |

**Usable right now without human action:** `claude`, `opencode` (2/5).
**Need a one-time human login (free options exist):** `gemini`, `qwen`, `codex`. Exact commands are in `docs/competition-backends-status.md` → "Login commands the human must run". After logging in, re-run `python scripts/smoke_backends.py` to confirm they flip to `OK`.

> **PATH note for the future runner:** the four non-claude CLIs live in `~/.npm-global/bin` (system npm prefix is root-owned; no sudo). Any process that shells out to them must have `~/.npm-global/bin` on `PATH`.

## Tester scores (0–5)

Validated by a dedicated tester agent against a `614f5fd` baseline worktree (delta analysis).

| Job | Correctness | Convention-fit | Test-pass | Notes |
|---|---|---|---|---|
| **J0** | 5 | 5 | 5 | claude path byte-for-byte preserved; precedence correct; no API key written; ruff clean; mypy/pytest delta 0. Ships no unit tests of its own (diagnostic code). |
| **J1** | 5 | 5 | 5 | fully additive + idempotent; `Decimal`/`NUMERIC(12,2)`; `conn()` + `insert_audit`; verified live (DB spot-check + double-run idempotency). |

### Gate results on the merged branch
- `ruff check helm tests scripts` → **PASS** (clean).
- `mypy helm` → 141 errors / 20 files **= identical to the `614f5fd` baseline (delta 0)**. No new type errors introduced (see Deviations).
- `pytest -q` → **34 passed, 0 failed** (delta 0 vs baseline).
- `init_schema()` ×2 → idempotent PASS.
- `python scripts/migrate_competition.py` ×2 → idempotent PASS (2nd run: 0 inserts, "nothing to do"; wallet not reset).
- DB spot-check → 5 new tables present; `competitor_id` present on all 5 existing tables; `house-claude` + ₹50,000 wallet present.
- `import helm.llm` + default-backend → resolves to `claude`; optional subscription smoke `{"ok": true}` in 8.2s, no API key.

## Deviations from the brief (all minor / justified)

1. **`mypy helm` does not pass cleanly — but this is a pre-existing repo baseline, not introduced here.** 141 errors / 20 files exist at the base commit `614f5fd` (dict_row row-factory typing cascading from `helm/data/store.py`, plus the anthropic SDK block `.text` union-attr in the untouched `_complete_api`). J0 + J1 add **zero** new errors (verified by delta against the baseline worktree). Fixing the 141-error baseline is out of scope for this run and could destabilize the live bot.
2. **Worker-A (J0) did not land in a separate worktree** — its commit `9eaf727` went directly onto `feat/competition-league` in the main checkout. Outcome is correct (parent `614f5fd`, disjoint files, verified), so no merge was needed for J0; only J1 was merged in.
3. **Worker-B (J1) worktree was initially based on a stale commit (`4b3ecfe`)** and was fast-forwarded (`--ff-only`) to `614f5fd` before committing. Final commit `5970015` correctly has parent `614f5fd` (verified). No content lost.
4. **CLIs installed to a user-local npm prefix (`~/.npm-global`)** instead of the system global, because `/usr/lib/node_modules` is root-owned and no sudo is available unattended. Adapters honor `*_CLI_PATH` overrides.
5. **`helm/data/store.py` was listed as in-scope for J1 but needed no change** — the work fit entirely in `schema.sql` + the new migration script. `init_schema()` already reads `schema.sql`, so no store-layer change was required.
6. **`migrate_competition.py` stamps only `signals`/`decisions`/`paper_trades`** (exactly what the J1 brief required). `daily_state` is empty (no-op) and `metrics_snapshots` has 2 legacy rows left with `competitor_id = NULL` — consistent with the migration's stated contract. Decide later whether to stamp those 2 legacy snapshot rows.
7. **External activity during the run (not ours):** the live autonomy loop appended smoke-test entries to `RELEASES.md` (dummy SHA `deadbeefca`) while this build ran. That working-tree change was left untouched and is **not** part of any commit on this branch. Autonomy was not paused.

## Known follow-ups surfaced (out of J0/J1 scope — for J2+)
- Live cron writers (`scan_signals` / `decide_signals` / `paper_execute`) still insert rows with `competitor_id = NULL`. Wiring them to stamp `house-claude` (and, later, the right competitor) is J2+ work — the nullable column was designed for exactly this transition.
- The three NEEDS-AUTH backends require a one-time human login before they can compete.

---

## NEXT: J2–J5 (awaiting human go)

Do **not** start these until the human explicitly approves. Suggested order (from the brief's phase map):

1. **Human action first:** log in to `gemini`, `qwen`, `codex` (free options preferred — see `docs/competition-backends-status.md`), then re-run `python scripts/smoke_backends.py` and confirm how many of the 5 backends are `OK`. The cohort can launch with whatever subset is authenticated (`claude` + `opencode` already work).
2. **J2 — freestyle runner:** each competitor emits its own OPEN/CLOSE/HOLD trade-intent JSON from a market snapshot, validated by the existing risk gate and routed through the `paper_execute` chokepoint into its own `competitor_wallets` row. Log every call to `agent_invocations`.
3. **J3 — weekly mandate + dynamic poller:** each agent declares ≤~15 symbols (`competitor_mandates`); a dynamic poller polls the union of all mandates' symbols via `yfinance` (`{SYM}.NS`).
4. **J4 — quota subsystem:** keep free-tier agents inside free quotas using `backend_quota_state`; on exhaustion → log + pause-until-reset + auto-resume.
5. **J5 — dashboard pages:** league leaderboard (equity per competitor) + a "how it thinks" reasoning view backed by `agent_invocations`.
6. Re-integrate, re-run the tester, commit after each job. Never push.

---

## UPDATE 2026-05-20 · J2 — Freestyle runner DONE

**Scope this run:** J2 only (the freestyle competition runner). $0 spend (subscription/opencode-free CLIs only; `ANTHROPIC_API_KEY` never set). Local commit only — not pushed. **NOT wired into cron** — going live is a human decision (each cycle spends backend quota). The incumbent `house-claude` scan→decide→execute cron pipeline is untouched.

### What J2 adds
- **`helm/competition/wallet.py`** — `competitor_wallet_state(id)`: per-competitor `WalletState`, recomputed from that competitor's `paper_trades` + its `competitor_wallets.initial_capital`. `sync_wallet_cache(id)` refreshes the denormalised `available_inr`/`realized_pnl_inr` columns for the dashboard. Money is `Decimal`.
- **`helm/competition/execute.py`** — the league chokepoint. `execute_competitor_open(...)` synthesises a `signals` row (`strategy='freestyle'`, stamped `competitor_id`, immediately consumed), runs the per-competitor risk gate, writes a `decisions` row, and on pass opens a `paper_trades` row — every row carries `competitor_id`. `close_competitor_position(...)` does discretionary early exit reusing the Zerodha-MIS charge model. Long-only v1.
- **`helm/competition/runner.py`** — one decision cycle per competitor: build snapshot (recent 1-min candles for the competitor's universe + its open positions + wallet) → call its backend via `helm.llm.complete_json` with a freestyle OPEN/CLOSE/HOLD actions schema + persona → log to `agent_invocations` → route actions through the execute path. Universe comes from this week's `competitor_mandates` row when present (J3 drop-in), else a default slice of `WATCHLIST`. Execution price = live yfinance LTP, falling back to the snapshot's last candle close.
- **`helm/orchestrator/risk.py`** — every check now takes optional `competitor_id`. `None` ⇒ the incumbent **house book** (`competitor_id IS NULL OR 'house-claude'`); a concrete id ⇒ that competitor's own rows. Each competitor gets independent open-position / daily-loss / cooldown / max-signals limits and is sized against its own isolated wallet.
- **`helm/wallet.py`** — house `wallet_state()` now filters to house rows (`HOUSE_TRADE_FILTER`) so league trades never leak into the incumbent's equity/sizing. With no competitors trading this is identical to the previous unfiltered query.
- **`helm/config.py`** — `HOUSE_COMPETITOR_ID` / `HOUSE_TRADE_FILTER` constants (single source of truth for "which rows are the house's").
- **`helm/data/store.py`** — `conn()` annotated `Connection[dict[str, Any]]` (type-only; matches the runtime `dict_row` factory). This fixed the dict_row typing cascade the J1 status flagged: **`mypy helm` 141 → 65 errors**.
- **`scripts/seed_competitors.py`** — idempotent: seeds the 4 freestyle competitors (`gemini-momentum`, `qwen-meanrev`, `codex-trend`, `opencode-range`) each with a distinct persona + isolated ₹50k wallet.
- **`scripts/run_competitors.py`** — cron-ready CLI (`--competitor`, `--backend`, `--dry-run`, `--force-window`). No-ops outside the trading window unless forced.

### Verification
- `ruff check helm scripts` → clean. `pytest -q` → 34 passed (delta 0). `mypy helm` → 65 (well below the 141 baseline; net improvement).
- **Deterministic functional test:** open→close a competitor trade; verified isolated wallet debit/credit, net-P&L with charges, per-competitor cooldown block, and that the house wallet/risk count is unaffected by competitor trades (leak found and fixed mid-run).
- **Live LLM integration:** real `opencode` backend produced a valid OPEN action (with stop/target/reason + commentary) from the live snapshot; runner resolved the live price, validated the stop, and logged the call to `agent_invocations` (ok=true, 23.8s). Test artifacts cleaned up — all 4 freestyle wallets left pristine at ₹50,000.

### Auth status unchanged (re-run `python scripts/smoke_backends.py` after any login)
Usable now: `claude` (incumbent), `opencode` (freestyle). Still NEEDS-AUTH: `gemini`, `qwen`, `codex` — they're seeded and will compete the moment their CLI is logged in.

### NEXT (still awaiting human go): J3 dynamic mandate+poller · J4 quota subsystem · J5 dashboard pages, then go-live (add `run_competitors.py` to cron) once the human approves.

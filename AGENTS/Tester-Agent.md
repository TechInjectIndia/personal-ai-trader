# Tester-Agent

## Mission

Verify that every shipped release is healthy on the live deployment — **both the new feature and the pre-existing pipeline**. If anything is broken, revert the commit and file a `bug_fix` task so the Engineer can retry. You are the autonomy loop's safety net.

## Cadence

- **Reactive trigger:** whenever `releases.status = 'deployed'` rows exist with `verified_ts IS NULL`. The cron polls every 5 minutes during weekdays.
- **Manual:** `python scripts/tester_run.py --release-id N` for ad-hoc.
- **One release at a time.** Lock by advisory key so two cron firings can't both verify the same row.

## Inputs

- One unverified release row (oldest first → FIFO).
- The release's commit SHA, the linked task, and the relevant `RELEASES.md` block.
- The repo at HEAD (which should equal the release commit unless another release shipped in between).

## Test stages (run in order; fail-fast)

1. **Static gates** — `ruff check helm scripts` + `pytest -q tests/`. Same gates the engineer ran, but re-verified.
2. **Pipeline integration smoke** — `tests/test_pipeline_smoke.py`:
   - Inserts a synthetic 1-min candle series for one symbol into the test schema.
   - Runs `scan_signals.main(['--no-decide', '--force-window'])` against it (offline DB).
   - Asserts a signal row was emitted.
   - Calls `decide_signal_inline(sig_id)` with `LLM_MODE` mocked to return a deterministic TAKE.
   - Asserts a `paper_trades` row landed and respects the dynamic position cap.
   - Calls `manage_positions.evaluate_open_trades()` with a moving-price candle and asserts target/stop hit logic.
3. **Dashboard reachability** — `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8501` must be `200`.
4. **PM2 health** — `pm2 jlist` must show `helm-dashboard` `online` with `restarts < 50`.
5. **Pre-existing-feature spot check** — for each of: ORB-15m, ORB-5m, VWAP reclaim, gap fade — verify the strategy class still imports and `scan(...)` returns a `Signal | None` without raising on a sample candle list.
6. **24-h P&L sanity** (only when a full day has elapsed since the previous verified release): the post-release day's realised P&L must not be **worse than -₹500**. If so, revert as a regression even though tests pass.

## Outputs

- **On pass:** `releases.status = 'verified'`, `tester_notes` summarises which stages passed and any soft warnings.
- **On fail:** 
  1. `git revert <commit_sha>` non-interactively, capture the revert SHA.
  2. `releases.status = 'reverted'`, `revert_sha` set, `tester_notes` quotes the failing assertion + stack.
  3. New `agent_tasks` row, type `bug_fix`, priority 5, `parent_task_id` = the original task. Spec includes the failing test name + stderr excerpt + the reverted file paths.
  4. PM2 reload if a config or dashboard file changed.
- One `agent_runs` row with stage-by-stage trace.

## Decision principles

1. **Tests are not optional.** If the integration smoke can't run (e.g. fixture broken), revert and file the failure — don't skip.
2. **Revert > forward-fix.** Even if the bug looks small, revert and let the Engineer reship. This keeps `main` always-green.
3. **Quote, don't paraphrase, the failure.** Bug-fix tasks must carry the exact assertion/stack so the Engineer reproduces.
4. **Soft warnings are not failures.** Slow tests (>30s) get noted in `tester_notes` but don't block verification.
5. **Don't revert the revert.** If the post-revert state still fails, the loop stops itself: file `needs_human` instead of cycling.

## Skills & plugins

- Standard `pytest`, `ruff`, `git`, `pm2` via `helm.agents.base.run_cmd`.
- `curl` for dashboard reachability.
- No LLM calls in the happy path — Tester is deterministic. LLM is invoked only to *summarise* the verdict into `tester_notes` (one short paragraph), with `helm.llm.complete_json` and a tiny schema.
- Recommended Claude Code skills (interactive runs):
  - `/review` if a release passes but produced an unusually large diff.
  - `/security-review` automatically if the changed files include `risk.py`, `paper_execute.py`, `wallet.py`, or anything under `helm/orchestrator/`.

## Success criteria

- 0 reverts that the next Engineer iteration couldn't fix on the second try.
- 100% of `main` HEAD commits are either `verified` or `reverted` — never long-lived `deployed` with the tester silent.
- Median time from `release_deployed` to terminal state < 10 minutes.

## Failure modes & escalation

- **Revert itself fails** (e.g. merge conflict in the revert): `release.status='failed'`, `tester_notes` captures git output, audit `agents/revert_failed`, `needs_human` task filed immediately.
- **Two reverts in a row**: pause autonomous loop — Tester sets a `settings` row `autonomy_paused=true`. PM and Engineer crons check this flag at start.
- **Synthetic fixture missing**: Tester refuses to verify and files `needs_human` with the missing fixture detail.

## Observability

- `agent_runs.trace` carries per-stage pass/fail JSON so the dashboard can render a grid.
- The dashboard "Self-Improvement Loop" page shows for each release: stages run, durations, and the diff that landed (or got reverted).
- `audit` rows on every revert with the original commit SHA, the revert SHA, and the linked bug-fix task id — searchable from the dashboard.

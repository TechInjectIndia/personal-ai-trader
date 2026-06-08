# FRD F3 — Daily Post-Close Learning Cadence

_Phase 1 · Owner: PM/ops · Status: ✅ SHIPPED 2026-06-08_

## 1. Problem
The PM→Engineer→Tester learning loop ran only on Sundays. Decisions took up to a week to land — too slow to iterate toward profitability.

## 2. Goal & success metrics
Run the full learning loop **post-close every trading day (Mon–Fri)** so retros → proposals → tasks → releases → verification complete within ~3h of the close and are live the next open.
- Success: ≥1 PM + Engineer + Tester run per trading day; a daily report file per day; no market-hours code edits; no live-bot breakage.

## 3. HLD
Pure scheduling change. The loop scripts (`retro_trades.py`, `pm_review.py`, `engineer_run.py`, `tester_run.py`) have **no internal day-of-week guard** — cadence is 100% cron timing — so moving the cron is sufficient. Edits land after 15:30 IST (market shut), the Tester verifies/reverts before the next open. Mandate revision and a cumulative week-in-review stay weekly.

## 4. LLD (as implemented)
**Crontab** (`crontab -l`), all UTC, Mon–Fri (`* * 1-5`):
| IST | Step | Cron |
|---|---|---|
| 15:30–16:15 | `retro_trades.py --force-window` | `*/15 10 * * 1-5` |
| 16:30 | `pm_review.py` | `0 11 * * 1-5` |
| 16:30–18:20 | `engineer_run.py --once` | `*/10 11-12 * * 1-5` |
| 16:35–19:25 | `tester_run.py` | `5-55/10 11-13 * * 1-5` |
| 19:00 | `weekend_report.py` (today) | `30 13 * * 1-5` |

**Weekly (Sunday):** `plan_mandates.py --next-week` (`0 3 * * 0`); `weekend_report.py --weekly` (`0 10 * * 0`).

**`scripts/weekend_report.py`** — default date changed from most-recent-Sunday → **today**; added `--weekly` for the cumulative Sunday summary; retitled "Weekend" → "Self-improvement loop". One file per day in `logs/weekend-reports/`.

Prior crontab backed up to `logs/crontab.backup.<ts>`.

## 5. Test / verification
- Confirmed no day-guards via grep of loop scripts.
- `weekend_report.py` runs in default (today), `--date`, and `--weekly` modes (ruff clean).
- Cron installed and the post-close block verified present.

## 6. Rollout / revert
Live now. Revert = `crontab logs/crontab.backup.<ts>` (restores Sunday-only). Pause without reverting: `INSERT INTO settings(key,value,updated_by) VALUES('autonomy_paused','true','manual')`.

## 7. Risks
- **More LLM spend** (5× cadence). Mitigation: retro capped to 4 ticks/day; PM defers when no new trades/proposals.
- **Overnight regression shipped.** Mitigation: Tester pytest gate + revert before open; no market-hours edits.

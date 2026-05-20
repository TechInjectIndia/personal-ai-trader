"""
Freestyle competition runner — cron entry point (one decision cycle).

Runs a decision cycle for each active freestyle competitor: snapshot → backend
call → actions → per-competitor risk gate → isolated wallet. The incumbent
'house-claude' is NOT run here — it keeps its own scan/decide/execute cron path.

NOT yet wired into crontab — going live is a human decision (each cycle spends
backend quota). Intended cadence once enabled: every 5 minutes in-window,
mirroring the house decide loop:

  */5 3-9 * * 1-5  run_in_venv.sh scripts/run_competitors.py >> logs/competitors.log 2>&1

Like the other cron scripts, it no-ops silently outside the trading window and
on weekends unless --force-window is given.

Usage:
  python scripts/run_competitors.py                       # all freestyle agents, in-window
  python scripts/run_competitors.py --force-window        # ignore the clock (manual)
  python scripts/run_competitors.py --competitor gemini-momentum --force-window
  python scripts/run_competitors.py --dry-run --force-window   # decide, don't book
  python scripts/run_competitors.py --competitor opencode-range --backend claude --force-window
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from helm.competition.runner import (
    Competitor,
    freestyle_competitors,
    get_competitor,
    run_competitor_cycle,
)
from helm.config import TRADING_END, TRADING_START
from helm.data.store import insert_audit

IST = ZoneInfo("Asia/Kolkata")
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _within_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return TRADING_START <= now.time() <= TRADING_END


def _select(competitor_id: str | None) -> list[Competitor]:
    if competitor_id:
        comp = get_competitor(competitor_id)
        if comp is None:
            print(f"[competitors] no competitor id {competitor_id!r}", file=sys.stderr)
            return []
        if comp.autonomy_level != "freestyle":
            print(f"[competitors] {competitor_id!r} is not freestyle "
                  f"(autonomy={comp.autonomy_level}); skipping", file=sys.stderr)
            return []
        return [comp]
    return freestyle_competitors()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--competitor", default=None, help="run just one competitor id")
    p.add_argument("--backend", default=None,
                   help="override the backend for this run (testing only)")
    p.add_argument("--dry-run", action="store_true",
                   help="call the backend and decide, but do not book trades")
    p.add_argument("--force-window", action="store_true",
                   help="bypass the trading-window check (manual runs)")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)
    now = datetime.now(IST)

    def _say(msg: str) -> None:
        if args.force_window or args.dry_run:
            print(f"[competitors] {msg}", flush=True)

    if not args.force_window and not _within_window(now):
        return 0
    if not _within_window(now):
        _say("outside trading window — proceeding anyway (--force-window)")

    competitors = _select(args.competitor)
    if args.backend:
        competitors = [replace(c, backend=args.backend) for c in competitors]

    if not competitors:
        _say("no freestyle competitors to run")
        return 0

    _say(f"running {len(competitors)} competitor(s) "
         f"now_ist={now.strftime('%H:%M:%S')} dry_run={args.dry_run}")

    totals = {"opened": 0, "closed": 0, "held": 0, "blocked": 0, "errors": 0, "ok": 0}
    for comp in competitors:
        res = run_competitor_cycle(comp, dry_run=args.dry_run)
        totals["ok"] += int(res.ok)
        for k in ("opened", "closed", "held", "blocked", "errors"):
            totals[k] += getattr(res, k)
        status = "ok" if res.ok else "FAIL"
        print(f"  {comp.id:<18} [{comp.backend}] {status}: {res.message[:160]}")

    insert_audit("run_competitors", "run_summary",
                 {**totals, "considered": len(competitors), "dry_run": args.dry_run})
    _say(f"done · {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Weekly mandate planner — each freestyle competitor picks its symbols for the week.

For every active freestyle competitor, ensure a `competitor_mandates` row exists
for the current week (Monday-anchored): if absent, the competitor's own backend
is asked to choose ≤15 symbols from the candidate universe (helm.config
TRADABLE_UNIVERSE), framed by its persona. Idempotent within a week — an
existing mandate is left untouched unless --force.

The freestyle runner reads this week's mandate automatically; the dynamic poller
(scripts/poll_competition.py) then polls whatever extra symbols were chosen.

NOT yet wired into crontab — going live is a human decision (each plan spends one
backend call per competitor). Intended cadence once enabled: Monday morning,
ahead of the trading week, e.g.:

  30 3 * * 1  run_in_venv.sh scripts/plan_mandates.py >> logs/mandates.log 2>&1

$0 spend: backend calls run via the CLI backends (subscription / free gateways);
ANTHROPIC_API_KEY is never required or set.

Usage:
  python scripts/plan_mandates.py                         # all active freestyle agents
  python scripts/plan_mandates.py --competitor gemini-momentum
  python scripts/plan_mandates.py --force                 # regenerate even if present
  python scripts/plan_mandates.py --dry-run               # generate + print, do not persist
  python scripts/plan_mandates.py --competitor opencode-range --backend claude
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from helm.competition.mandate import (
    current_week_start,
    ensure_mandate,
    generate_mandate,
)
from helm.competition.runner import Competitor, freestyle_competitors, get_competitor
from helm.data.store import insert_audit

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _select(competitor_id: str | None) -> list[Competitor]:
    if competitor_id:
        comp = get_competitor(competitor_id)
        if comp is None:
            print(f"[mandates] no competitor id {competitor_id!r}", file=sys.stderr)
            return []
        if comp.autonomy_level != "freestyle":
            print(f"[mandates] {competitor_id!r} is not freestyle "
                  f"(autonomy={comp.autonomy_level}); skipping", file=sys.stderr)
            return []
        return [comp]
    return freestyle_competitors()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--competitor", default=None, help="plan just one competitor id")
    p.add_argument("--backend", default=None,
                   help="override the backend for this run (testing only)")
    p.add_argument("--force", action="store_true",
                   help="regenerate even if a mandate already exists this week")
    p.add_argument("--dry-run", action="store_true",
                   help="generate + print the mandate but do not persist it")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    competitors = _select(args.competitor)
    if args.backend:
        competitors = [replace(c, backend=args.backend) for c in competitors]
    if not competitors:
        print("[mandates] no freestyle competitors to plan")
        return 0

    wk = current_week_start()
    print(f"[mandates] week_start={wk.isoformat()} competitors={len(competitors)} "
          f"dry_run={args.dry_run} force={args.force}")

    planned = 0
    for comp in competitors:
        if args.dry_run:
            plan = generate_mandate(comp)
            if plan["paused"]:
                print(f"  {comp.id:<18} [{comp.backend}] PAUSED (quota) — no plan")
                continue
            print(f"  {comp.id:<18} [{comp.backend}] "
                  f"{'(fallback) ' if plan['fellback'] else ''}"
                  f"universe={plan['universe']}")
            print(f"      rationale: {plan['rationale'][:160]}")
            continue
        res = ensure_mandate(comp, force=args.force)
        planned += int(res["created"])
        if res["paused"]:
            tag = "paused(quota)"
        elif res["created"]:
            tag = "planned(fallback)" if res["fellback"] else "planned"
        else:
            tag = "kept"
        print(f"  {comp.id:<18} [{comp.backend}] {tag}: {res['universe']}")

    if not args.dry_run:
        insert_audit("plan_mandates", "run_summary",
                     {"week_start": wk.isoformat(), "considered": len(competitors),
                      "planned": planned, "forced": args.force})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

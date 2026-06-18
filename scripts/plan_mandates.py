"""
Weekly mandate planner — each freestyle competitor picks its symbols for the week.

For every active freestyle competitor, ensure a `competitor_mandates` row exists
for the current week (Monday-anchored): if absent, the competitor's own backend
is asked to choose ≤15 symbols from the candidate universe (helm.config
TRADABLE_UNIVERSE), framed by its persona. Idempotent within a week — an
existing mandate is left untouched unless --force.

The freestyle runner reads this week's mandate automatically; the dynamic poller
(scripts/poll_competition.py) then polls whatever extra symbols were chosen.

Cadence: SUNDAY morning (markets closed), planning the UPCOMING week so mandates
are in place before Monday's open. The runner reads a mandate via the current
week's Monday, so a weekend plan MUST target next week — use `--next-week`:

  0 3 * * 0  run_in_venv.sh scripts/plan_mandates.py --next-week >> logs/mandates.log 2>&1

$0 spend: backend calls run via the CLI / OpenRouter free backends; the Anthropic
API key is never required.

Usage:
  python scripts/plan_mandates.py                         # this week (Mon-Fri runner default)
  python scripts/plan_mandates.py --next-week             # upcoming Monday (weekend planner)
  python scripts/plan_mandates.py --week-start 2026-05-25 # a specific week (its Monday)
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

from datetime import date

from helm.competition.mandate import (
    current_week_start,
    ensure_mandate,
    generate_mandate,
    next_week_start,
    week_start,
)
from helm.competition.runner import Competitor, freestyle_competitors, get_competitor
from helm.data.store import insert_audit
from helm.markets import enabled_markets

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
    p.add_argument("--model", default=None,
                   help="override the model (pairs with --backend; when --backend "
                        "is given without --model, the model resets to that "
                        "backend's own default)")
    p.add_argument("--force", action="store_true",
                   help="regenerate even if a mandate already exists this week")
    p.add_argument("--dry-run", action="store_true",
                   help="generate + print the mandate but do not persist it")
    p.add_argument("--next-week", action="store_true",
                   help="plan for the UPCOMING Monday (next calendar week) — use "
                        "on weekends so mandates are ready before the week opens")
    p.add_argument("--week-start", default=None, metavar="YYYY-MM-DD",
                   help="plan for the week containing this date (normalised to its "
                        "Monday); overrides --next-week")
    p.add_argument("--market", default=None,
                   help="plan just one market (IN/US/CRYPTO); default = all enabled")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    # Resolve the target week: explicit --week-start > --next-week > current week.
    if args.week_start:
        wk = week_start(date.fromisoformat(args.week_start))
    elif args.next_week:
        wk = next_week_start()
    else:
        wk = current_week_start()

    competitors = _select(args.competitor)
    if args.backend:
        # See run_competitors.py: a backend override must drop the competitor's
        # vendor model, else e.g. --backend claude passes --model gemini-2.5-flash
        # to the claude CLI and it 404s. Reset to --model, else the backend default.
        competitors = [replace(c, backend=args.backend, model=args.model)
                       for c in competitors]
    elif args.model:
        competitors = [replace(c, model=args.model) for c in competitors]
    if not competitors:
        print("[mandates] no freestyle competitors to plan")
        return 0

    markets = [args.market] if args.market else [m.key for m in enabled_markets()]
    print(f"[mandates] week_start={wk.isoformat()} competitors={len(competitors)} "
          f"markets={markets} dry_run={args.dry_run} force={args.force}")

    planned = 0
    for mkt in markets:
        for comp in competitors:
            if args.dry_run:
                plan = generate_mandate(comp, wk_start=wk, market=mkt)
                if plan["paused"]:
                    print(f"  [{mkt}] {comp.id:<18} [{comp.backend}] PAUSED (quota) — no plan")
                    continue
                print(f"  [{mkt}] {comp.id:<18} [{comp.backend}] "
                      f"{'(fallback) ' if plan['fellback'] else ''}"
                      f"universe={plan['universe']}")
                print(f"      rationale: {plan['rationale'][:160]}")
                continue
            res = ensure_mandate(comp, force=args.force, wk_start=wk, market=mkt)
            planned += int(res["created"])
            if res["paused"]:
                tag = "paused(quota)"
            elif res["created"]:
                tag = "planned(fallback)" if res["fellback"] else "planned"
            else:
                tag = "kept"
            print(f"  [{mkt}] {comp.id:<18} [{comp.backend}] {tag}: {res['universe']}")

    if not args.dry_run:
        insert_audit("plan_mandates", "run_summary",
                     {"week_start": wk.isoformat(), "considered": len(competitors),
                      "markets": markets, "planned": planned, "forced": args.force})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

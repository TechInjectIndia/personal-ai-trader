"""
PM weekly-review CLI.

Cron path (Monday 06:00 IST via crontab) calls this with no args; manual
human runs add `--force` to bypass the "nothing new since last run" and
"unverified release in flight" skip rules.

Always exits 0 on a successful run — including the deferred no-op paths,
which are normal. Exits 1 only on an unexpected exception (LLM transport
failure, DB unreachable, etc.); the wrapper logs surface those.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/pm_review.py` directly (cron path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from helm.config import HOUSE_COMPETITOR_ID
from helm.agents.base import is_autonomy_paused
from helm.agents.pm import run_backlog_drain, run_weekly_review

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _print_one(result: dict) -> None:
    """Print a single agent's review summary."""
    who = result.get("competitor_id") or "house"
    if result.get("deferred"):
        print(f"[pm:{who}] deferred run_id={result['run_id']} — "
              f"{result.get('reason') or 'no-op'}")
        return

    tasks = result.get("tasks_created") or []
    accepted = result.get("proposals_accepted") or []
    rejected = result.get("proposals_rejected") or []
    print(
        f"[pm:{who}] run_id={result['run_id']} "
        f"tasks_created={tasks} "
        f"proposals_accepted={accepted} "
        f"proposals_rejected={rejected}"
    )
    if not (tasks or accepted or rejected) and result.get("reason"):
        print(f"[pm:{who}] no actions taken — {result['reason']}")


def _print_summary(result: dict | list) -> None:
    """Handle both the single-agent dict and the all-agents list."""
    if isinstance(result, list):
        for r in result:
            _print_one(r)
        print(f"[pm] reviewed {len(result)} agent(s)")
        return
    _print_one(result)


def _print_backlog_one(result: dict) -> None:
    """Print a single agent's backlog-drain summary."""
    who = result.get("competitor_id") or "house"
    print(
        f"[pm:{who}] backlog run_id={result['run_id']} "
        f"clusters_seen={result.get('clusters_seen', 0)} "
        f"tasks_created={result.get('tasks_created') or []} "
        f"accepted={result.get('proposals_accepted') or []} "
        f"superseded={len(result.get('proposals_superseded') or [])} "
        f"rejected={len(result.get('proposals_rejected') or [])}"
    )
    if result.get("reason"):
        print(f"[pm:{who}] {result['reason']}")


def _backlog_agent_ids(args: argparse.Namespace) -> list[str]:
    """Resolve which agents the backlog pass targets.

    --competitor X → just X; --all (or no target) → house + every active
    freestyle competitor."""
    target = args.competitor or args.competitor_id
    if target and not args.all:
        return [target]
    from helm.competition.competitors import freestyle_competitors
    return [HOUSE_COMPETITOR_ID] + [c.id for c in freestyle_competitors()]


def _run_backlog(args: argparse.Namespace) -> int:
    """One backlog-drain pass per targeted agent. Errors surface uniformly."""
    try:
        for aid in _backlog_agent_ids(args):
            result = run_backlog_drain(aid, max_clusters=args.max_clusters)
            _print_backlog_one(result)
    except Exception as exc:  # noqa: BLE001 — CLI surfaces all failures uniformly
        print(f"[pm] backlog error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run the PM weekly review.")
    p.add_argument("--force", action="store_true",
                   help="Bypass the deferral rules (no-new-data + unverified change).")
    p.add_argument("--competitor-id", default=None,
                   help="Review just this agent; default = house + all freestyle.")
    p.add_argument("--backlog", action="store_true",
                   help="Backlog-drain mode: dedup+rank the agent's whole open "
                        "proposal backlog and autonomously accept/reject the top "
                        "clusters (no weekly skip gates).")
    p.add_argument("--competitor", default=None,
                   help="Backlog mode: target just this agent id.")
    p.add_argument("--all", action="store_true",
                   help="Backlog mode: target house + every active freestyle "
                        "competitor (the default when no --competitor given).")
    p.add_argument("--max-clusters", type=int, default=3,
                   help="Backlog mode: max clusters to decide per pass (<=3).")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    if not args.force and is_autonomy_paused():
        print("[pm] autonomy paused — no-op (use --force to override)")
        return 0

    if args.backlog:
        return _run_backlog(args)

    try:
        result = run_weekly_review(force=args.force,
                                   competitor_id=args.competitor_id)
    except Exception as exc:  # noqa: BLE001 — CLI surfaces all failures uniformly
        print(f"[pm] error: {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

from helm.agents.pm import run_weekly_review

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _print_summary(result: dict) -> None:
    if result.get("deferred"):
        print(f"[pm] deferred run_id={result['run_id']} — {result.get('reason') or 'no-op'}")
        return

    tasks = result.get("tasks_created") or []
    accepted = result.get("proposals_accepted") or []
    rejected = result.get("proposals_rejected") or []
    print(
        f"[pm] run_id={result['run_id']} "
        f"tasks_created={tasks} "
        f"proposals_accepted={accepted} "
        f"proposals_rejected={rejected}"
    )
    if not (tasks or accepted or rejected) and result.get("reason"):
        print(f"[pm] no actions taken — {result['reason']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Run the PM weekly review.")
    p.add_argument("--force", action="store_true",
                   help="Bypass the deferral rules (no-new-data + unverified release).")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    try:
        result = run_weekly_review(force=args.force)
    except Exception as exc:  # noqa: BLE001 — CLI surfaces all failures uniformly
        print(f"[pm] error: {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

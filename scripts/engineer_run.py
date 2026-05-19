"""
CLI entry point for the Product-Engineer agent.

Cron schedule (Mon–Fri every 30 minutes) shells in here with no flags. Manual
invocations use ``--once`` (default) to process exactly one task and exit so
the operator can eyeball the result before the next claim.

Exit codes:
  0 — success, or a clean no-op (nothing to claim / backpressure)
  1 — a task was claimed and ended in ``failed``

Standard output is suppressed under ``--quiet``; cron uses that so logs/
engineer.log only carries failures and explicit traces.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Project root onto sys.path so `from helm...` resolves under cron.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from helm.agents.engineer import process_one_task  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def main() -> int:
    p = argparse.ArgumentParser(description="Run the engineer agent once.")
    p.add_argument("--once", action="store_true", default=True,
                   help="Process at most one task and exit (default).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress stdout on success; failures still print.")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    result = process_one_task()

    if result is None:
        if not args.quiet:
            print("[engineer] no task claimed (queue empty or backpressure)")
        return 0

    if result.get("ok"):
        if not args.quiet:
            sha = (result.get("commit_sha") or "")[:10]
            print(f"[engineer] task {result['task_id']} → done "
                  f"release={result.get('release_id')} sha={sha} "
                  f"summary={result.get('summary')}")
        return 0

    # Failure path — always print, even with --quiet.
    print(f"[engineer] task {result.get('task_id')} → failed "
          f"reason={result.get('reason')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

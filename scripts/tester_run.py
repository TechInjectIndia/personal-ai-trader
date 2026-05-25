"""
Tester agent CLI — verifies one or all unverified releases.

Usage:
  python scripts/tester_run.py                  # process every deployed release
  python scripts/tester_run.py --release-id 42  # verify just release 42

Exit codes:
  0  every release processed without needing a revert
  1  one or more releases failed verification and were reverted (or escalated)
  2  fatal setup error (e.g. unknown release id)

The script is safe to call from cron. It uses a Postgres advisory lock
(`process_unverified()` handles that internally) so overlapping cron firings
don't double-verify the same row.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Cron working dir is `scripts/`; make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from helm.agents.tester import (  # noqa: E402
    STAGES_ORDER,
    process_unverified,
    process_unverified_config,
    verify_release,
)
from helm.agents.base import (  # noqa: E402
    git_revert,
    is_autonomy_paused,
    mark_release_reverted,
    create_task,
    pm2_reload,
)
from helm.data.store import conn  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _print_result(release_id: int, result: dict) -> None:
    """Pretty-print stage-by-stage outcome to stdout."""
    print(f"\n── release {release_id} ─────────────────────────────")
    for stage in STAGES_ORDER:
        s = result["stages"].get(stage)
        if not s:
            print(f"  [{stage:<24}] (not run)")
            continue
        flag = "PASS" if s["ok"] else "FAIL"
        print(f"  [{stage:<24}] {flag}  {s['duration_ms']}ms")
        if not s["ok"]:
            excerpt = (s.get("output_excerpt") or "").splitlines()
            for line in excerpt[-15:]:
                print(f"      {line}")
            if s.get("failure_reason"):
                print(f"      ↳ {s['failure_reason']}")
    overall = "PASSED" if result["passed"] else "FAILED"
    print(f"  → {overall}")
    if result["failure_reason"]:
        print(f"    failure_reason: {result['failure_reason']}")


def _verify_single(release_id: int) -> int:
    """Verify one release in the same shape as process_unverified.

    Mirrors the orchestration of process_unverified but for a single ID so
    `--release-id N` can be used both for dry-runs and for forced re-tries.
    """
    with conn() as c:
        rel = c.execute(
            "SELECT * FROM releases WHERE id=%s", (release_id,),
        ).fetchone()
    if rel is None:
        print(f"[tester] no release id={release_id}", file=sys.stderr)
        return 2

    result = verify_release(release_id)
    _print_result(release_id, result)

    if result["passed"]:
        # Read-only dry runs of a single ID don't move state — but for parity
        # with the cron path we still mark verified when the user explicitly
        # asks for it.
        from helm.agents.base import mark_release_verified  # local import
        from helm.agents.tester import _summarise_notes
        mark_release_verified(release_id, notes=_summarise_notes(rel, result))
        print(f"[tester] release {release_id} marked verified")
        return 0

    # Failure → revert + bug-fix task, exactly as in process_unverified.
    from helm.agents.tester import (
        _file_bug_task,
        _mark_release_failed,
        _release_touched_pm2_paths,
        _summarise_notes,
    )
    notes = _summarise_notes(rel, result)
    try:
        revert_sha = git_revert(rel["commit_sha"])
    except Exception as exc:  # noqa: BLE001 — CLI surface, report and exit
        msg = f"git revert failed: {exc!r}"
        _mark_release_failed(release_id, notes=msg)
        create_task(
            created_by="tester",
            task_type="needs_human",
            priority=5,
            parent_task_id=rel["task_id"],
            title=f"revert failed for release {release_id}",
            rationale=msg[:500],
            spec={"release_id": release_id,
                  "commit_sha": rel["commit_sha"],
                  "git_output": msg[:1500]},
        )
        print(f"[tester] {msg}", file=sys.stderr)
        return 1

    mark_release_reverted(release_id, revert_sha=revert_sha, notes=notes)
    bug_id = _file_bug_task(rel, result)
    if _release_touched_pm2_paths(rel):
        pm2_reload()
    print(f"[tester] release {release_id} reverted "
          f"(revert_sha={revert_sha[:10]}, bug_fix task={bug_id})")
    return 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--release-id", type=int, default=None,
                   help="Verify just this release; default = every unverified one")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    if args.release_id is not None:
        # Explicit human-driven single verify still runs even when paused.
        return _verify_single(args.release_id)

    if is_autonomy_paused():
        print("[tester] autonomy paused — no-op")
        return 0

    # Batch path: each loop handles its own locking, ordering, and the
    # two-reverts-in-a-row autonomy-pause guard internally. Run the house
    # release loop first, then the freestyle config-version loop.
    count = process_unverified()
    print(f"[tester] processed {count} release(s)")
    config_count = process_unverified_config()
    print(f"[tester] processed {config_count} config version(s)")

    # Exit 1 if anything was reverted/failed (releases OR config versions) in
    # the last 5 minutes; 0 if all green or no work. Neither loop returns the
    # breakdown directly, so we re-inspect via the tables.
    with conn() as c:
        row = c.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status IN ('reverted','failed')
                               AND COALESCE(reverted_ts, verified_ts) >= now() - interval '5 minutes'
                              ) AS recent_bad
            FROM releases
            """
        ).fetchone()
        crow = c.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status IN ('reverted','failed')
                               AND COALESCE(reverted_ts, verified_ts) >= now() - interval '5 minutes'
                              ) AS recent_bad
            FROM competitor_config_versions
            """
        ).fetchone()
    bad = int(row["recent_bad"] or 0) + int(crow["recent_bad"] or 0)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

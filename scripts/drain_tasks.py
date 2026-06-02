"""
Drain the existing ``agent_tasks`` queue — Tester ⇄ Engineer until empty.

Unlike ``drain_backlog.py`` (which gates on open *proposals* and runs the PM
to mint new tasks), this drains the tasks the PM has ALREADY queued. The PM is
not involved: we just run the Engineer to ship each open task and the Tester to
verify/revert what shipped, looping until the open-task queue is empty, the
iteration budget is spent, or the loop makes no progress.

Each iteration:
  1. Tester sweeps unverified releases + config versions. This is FIRST so the
     two-in-flight backpressure gate is cleared before the Engineer tries to
     claim — otherwise process_one_task() no-ops on a full release slot.
  2. Engineer claims + ships the next open task (its own ruff/pytest gates run;
     a bad mutator now fails cleanly thanks to the ast.parse guard in _write).
  3. Progress accounting. If an iteration claims nothing, verifies nothing, and
     leaves the open count unchanged for STALL_LIMIT iterations in a row, we
     stop rather than spin (remaining tasks are unclaimable or wedged behind an
     unverifiable release).

Autonomy pause is honoured (global and the per-agent trip the Tester sets after
repeated reverts) — if it flips on mid-drain we stop and report.

  python scripts/drain_tasks.py                 # drain all, default caps
  python scripts/drain_tasks.py --max-iterations 400
  python scripts/drain_tasks.py --limit 10      # ship at most 10 tasks then stop

Exit codes:
  0  drained cleanly / paused no-op / hit a cap
  1  an unexpected error bubbled up
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Cron/manual: make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from helm.agents.base import is_autonomy_paused  # noqa: E402
from helm.data.store import conn  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# Consecutive no-progress iterations tolerated before we declare the queue
# wedged and stop. One stalled pass can happen legitimately (a release needs a
# second Tester pass); several in a row means nothing claimable is left.
STALL_LIMIT = 3


def _open_task_count() -> int:
    with conn() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM agent_tasks WHERE status = 'open'"
        ).fetchone()
    return int(row["n"])


def _terminal_counts() -> dict[str, int]:
    with conn() as c:
        rows = list(c.execute(
            "SELECT status, count(*) AS n FROM agent_tasks GROUP BY status"
        ))
    return {r["status"]: int(r["n"]) for r in rows}


def main() -> int:
    p = argparse.ArgumentParser(
        description="Drain the open agent_tasks queue (Tester ⇄ Engineer).")
    p.add_argument("--max-iterations", type=int, default=400,
                   help="Safety cap on Tester⇄Engineer loops (default 400).")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after this many tasks ship successfully.")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    if is_autonomy_paused():
        print("[drain-tasks] autonomy paused globally — no-op "
              "(clear settings 'autonomy_paused' to drain)")
        return 0

    # Local imports so a paused/no-op exit never pays the heavy agent imports.
    from helm.agents.engineer import process_one_task
    from helm.agents.tester import process_unverified, process_unverified_config

    start_open = _open_task_count()
    print(f"[drain-tasks] starting — {start_open} open task(s) in queue")

    shipped = 0
    failed = 0
    stall = 0
    prev_open = start_open

    try:
        for i in range(1, args.max_iterations + 1):
            open_now = _open_task_count()
            if open_now == 0:
                print(f"[drain-tasks] queue empty after {i - 1} iteration(s)")
                break
            if is_autonomy_paused():
                print(f"[drain-tasks] autonomy paused mid-drain — stopping "
                      f"({open_now} open left)")
                break
            if args.limit is not None and shipped >= args.limit:
                print(f"[drain-tasks] hit --limit={args.limit} shipped — stopping "
                      f"({open_now} open left)")
                break

            # 1. Tester first — clear release/config backpressure.
            verified = process_unverified()
            cfg_verified = process_unverified_config()

            # 2. Engineer ships the next task.
            eng = process_one_task()
            if eng is None:
                eng_state = "no-claim"
            elif eng.get("ok"):
                eng_state = f"done(task={eng.get('task_id')})"
                shipped += 1
            else:
                eng_state = f"failed(task={eng.get('task_id')}: {eng.get('reason')})"
                failed += 1

            print(f"[drain-tasks] iter {i}: open={open_now} "
                  f"tester[rel={verified} cfg={cfg_verified}] "
                  f"engineer={eng_state} "
                  f"(shipped={shipped} failed={failed})")

            # 3. Progress accounting. Something happened if the Engineer claimed
            #    anything, the Tester moved anything, or the open count dropped.
            progressed = (eng is not None or verified or cfg_verified
                          or open_now < prev_open)
            stall = 0 if progressed else stall + 1
            prev_open = open_now
            if stall >= STALL_LIMIT:
                print(f"[drain-tasks] no progress for {STALL_LIMIT} iterations — "
                      f"stopping ({open_now} open left; likely unclaimable or "
                      f"wedged behind an unverifiable release)")
                break
        else:
            print(f"[drain-tasks] hit --max-iterations={args.max_iterations}")

        # Final Tester sweep so trailing releases don't sit unverified.
        tail_v = process_unverified()
        tail_c = process_unverified_config()
        if tail_v or tail_c:
            print(f"[drain-tasks] final tester sweep: rel={tail_v} cfg={tail_c}")
    except Exception as exc:  # noqa: BLE001 — one-shot CLI surfaces failures
        print(f"[drain-tasks] error: {exc}", file=sys.stderr)
        return 1

    counts = _terminal_counts()
    print(f"\n[drain-tasks] done. shipped={shipped} failed={failed}. "
          f"queue now: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

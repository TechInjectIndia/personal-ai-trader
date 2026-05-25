"""
Backlog-drain nudge — the ONE human action that clears an agent's existing
status='open' improvement_proposals backlog autonomously.

A human runs this once. For each targeted agent it loops:

    PM backlog mode  →  Engineer process_one_task  →  Tester process_unverified

until the agent has no status='open' proposals left OR --max-iterations is hit.
The PM agent itself decides accept/reject per (deduped, ranked) cluster — there
is NO human approval step. Backpressure is respected, not fought: the Engineer
no-ops when too many releases are unverified, and the Tester catches up on the
next iteration, so the loop drains gradually instead of stalling.

  python scripts/drain_backlog.py --all                 # house + all freestyle
  python scripts/drain_backlog.py --competitor house-claude
  python scripts/drain_backlog.py --all --dry-run       # print the plan, no writes
  python scripts/drain_backlog.py --all --max-iterations 50

--dry-run prints the cluster plan (top clusters per pass, recurrence, which
members would be superseded if accepted) WITHOUT touching the database — no PM,
no Engineer, no Tester, no status writes. It calls only the read-only clusterer.

Exit codes:
  0  drained cleanly (or dry-run, or paused no-op)
  1  an unexpected error bubbled up
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Cron/manual: make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from helm.agents.backlog import (  # noqa: E402
    cluster_open_proposals,
    open_proposal_count,
)
from helm.agents.base import is_autonomy_paused  # noqa: E402
from helm.config import HOUSE_COMPETITOR_ID  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# How many clusters the PM decides per backlog pass. Matches the PM's 0–3 shape.
CLUSTERS_PER_PASS = 3


def _target_agents(args: argparse.Namespace) -> list[str]:
    """Resolve which agents to drain: --competitor X → [X]; else house + all
    active freestyle competitors."""
    if args.competitor and not args.all:
        return [args.competitor]
    from helm.competition.competitors import freestyle_competitors
    return [HOUSE_COMPETITOR_ID] + [c.id for c in freestyle_competitors()]


def _dry_run(agents: list[str]) -> int:
    """Print the dedup/rank plan for each agent. NO DB WRITES.

    Shows the top CLUSTERS_PER_PASS clusters of the FIRST pass (what the PM
    would see next) plus the full backlog size, so the operator can eyeball the
    shortlist before nudging for real.
    """
    for aid in agents:
        clusters = cluster_open_proposals(aid)
        total_open = open_proposal_count(aid)
        print(f"\n══ {aid} — {total_open} open proposal(s), "
              f"{len(clusters)} cluster(s) ══")
        if not clusters:
            print("   (backlog empty — nothing to drain)")
            continue
        shown = clusters[:CLUSTERS_PER_PASS]
        for rank, cl in enumerate(shown, 1):
            rep = cl.representative
            others = [pid for pid in cl.member_ids
                      if pid != cl.representative_id]
            print(f"\n  [{rank}] cluster score={cl.score} "
                  f"(recurrence={cl.recurrence} × confidence={cl.confidence})")
            print(f"      category: {cl.category}")
            print(f"      representative #{cl.representative_id}: "
                  f"{(rep.get('title') or '')[:90]}")
            if others:
                preview = ", ".join(f"#{p}" for p in others[:12])
                more = "" if len(others) <= 12 else f" (+{len(others) - 12} more)"
                print(f"      would supersede if accepted: {preview}{more}")
            else:
                print("      (singleton — no members to supersede)")
        if len(clusters) > len(shown):
            print(f"\n  … {len(clusters) - len(shown)} more cluster(s) would be "
                  "handled in later passes.")
    print("\n[drain] dry-run only — no proposals, tasks, or releases changed.")
    return 0


def _drain_agent(aid: str, *, max_iterations: int) -> None:
    """Loop PM → Engineer → Tester for one agent until its open backlog is
    empty or the iteration budget is spent. Live DB writes happen here."""
    # Local imports so --dry-run never imports the live agents' heavier deps.
    from helm.agents.engineer import process_one_task
    from helm.agents.pm import run_backlog_drain
    from helm.agents.tester import process_unverified, process_unverified_config

    print(f"\n══ draining {aid} ══")
    for i in range(1, max_iterations + 1):
        remaining = open_proposal_count(aid)
        if remaining == 0:
            print(f"[drain:{aid}] backlog empty after {i - 1} iteration(s)")
            return

        # Per-agent pause honoured too (Tester trips this after 2 reverts).
        if is_autonomy_paused(aid):
            print(f"[drain:{aid}] autonomy paused for this agent — stopping "
                  f"({remaining} open left)")
            return

        pm_result = run_backlog_drain(aid, max_clusters=CLUSTERS_PER_PASS)
        tasks = pm_result.get("tasks_created") or []
        accepted = pm_result.get("proposals_accepted") or []
        superseded = pm_result.get("proposals_superseded") or []
        rejected = pm_result.get("proposals_rejected") or []
        clusters_seen = pm_result.get("clusters_seen", 0)
        print(f"[drain:{aid}] iter {i}: open={remaining} "
              f"clusters={clusters_seen} tasks={tasks} "
              f"accepted={len(accepted)} superseded={len(superseded)} "
              f"rejected={len(rejected)}")

        # If the PM made no progress (no clusters, no status moves), bail to
        # avoid spinning — either the backlog is empty or every cluster was
        # rejected as invalid. Re-check the count to be sure.
        moved = bool(tasks or accepted or superseded or rejected)
        if clusters_seen == 0 and not moved:
            print(f"[drain:{aid}] no clusters left to decide — done")
            return

        # Engineer applies any queued task (respects its own backpressure /
        # no-op when nothing to claim). Then Tester verifies what shipped so
        # the next PM pass isn't blocked by the one-change-in-flight gate.
        eng = process_one_task()
        if eng is None:
            print(f"[drain:{aid}] engineer: no task claimed (empty / backpressure)")
        else:
            ok = eng.get("ok")
            print(f"[drain:{aid}] engineer: task {eng.get('task_id')} → "
                  f"{'done' if ok else 'failed'}")

        verified = process_unverified()
        cfg_verified = process_unverified_config()
        if verified or cfg_verified:
            print(f"[drain:{aid}] tester: {verified} release(s), "
                  f"{cfg_verified} config version(s) processed")

        if not moved and eng is None:
            # Truly nothing happened this iteration and clusters exist but the
            # PM declined them all without status change — stop to avoid a spin.
            print(f"[drain:{aid}] no progress this iteration — stopping")
            return

    print(f"[drain:{aid}] hit --max-iterations={max_iterations}; "
          f"{open_proposal_count(aid)} open proposal(s) remain")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Autonomously drain an agent's open proposal backlog.")
    p.add_argument("--competitor", default=None,
                   help="Drain just this agent id (e.g. house-claude).")
    p.add_argument("--all", action="store_true",
                   help="Drain house + every active freestyle competitor "
                        "(default when no --competitor given).")
    p.add_argument("--max-iterations", type=int, default=40,
                   help="Per-agent safety cap on PM→Eng→Tester loops (default 40).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the cluster plan only; no DB writes, no agents run.")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)

    agents = _target_agents(args)

    if args.dry_run:
        return _dry_run(agents)

    if is_autonomy_paused():
        print("[drain] autonomy paused globally — no-op "
              "(clear settings 'autonomy_paused' to drain)")
        return 0

    try:
        for aid in agents:
            _drain_agent(aid, max_iterations=args.max_iterations)
    except Exception as exc:  # noqa: BLE001 — one-shot CLI surfaces failures
        print(f"[drain] error: {exc}", file=sys.stderr)
        return 1

    print("\n[drain] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

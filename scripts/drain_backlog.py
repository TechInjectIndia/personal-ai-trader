"""
Backlog-drain nudge — the ONE human action that clears an agent's existing
status='open' improvement_proposals backlog autonomously.

A human runs this once. For each targeted agent it loops:

    PM backlog mode  →  Engineer process_one_task  →  Tester process_unverified

until the agent has no status='open' proposals left OR --max-iterations is hit.
Each PM pass reads a RANKED BATCH of raw open proposals and consolidates them
SEMANTICALLY — accepting the best distinct ideas (one task each), superseding
restatements, and rejecting noise. There is NO human approval step and NO
reliance on lexical clustering for the dedup (restatements share too few tokens
for Jaccard/TF-IDF to catch). Because a single pass can supersede/reject the
whole long tail at once, the backlog drains in far fewer passes than the old
one-cluster-per-pass shape. Backpressure is respected, not fought: the Engineer
no-ops when too many releases are unverified, and the Tester catches up on the
next iteration, so the loop drains gradually instead of stalling.

  python scripts/drain_backlog.py --all                 # house + all freestyle
  python scripts/drain_backlog.py --competitor house-claude
  python scripts/drain_backlog.py --all --dry-run       # print the plan, no writes
  python scripts/drain_backlog.py --all --max-iterations 50

--dry-run prints an APPROXIMATE lexical grouping (read-only clusterer, no LLM)
plus a note that the real dedup is decided semantically by the PM at drain time.
It does NOT touch the database — no PM, no Engineer, no Tester, no status
writes — and it does NOT predict the actual accept/supersede plan, which only
the PM produces from the full proposal text.

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

# How many NEW tasks the PM may accept per backlog pass. Matches the PM's 0–3
# shape so the Engineer isn't flooded; supersede/reject in the same pass are
# unbounded, so a single batch can still collapse the whole long tail.
ACCEPTS_PER_PASS = 3

# How many APPROXIMATE lexical clusters to show in the --dry-run preview. The
# preview is non-authoritative (the PM dedups semantically at drain time), so
# this is purely a "size up the backlog" hint.
DRY_RUN_PREVIEW_CLUSTERS = 12


def _target_agents(args: argparse.Namespace) -> list[str]:
    """Resolve which agents to drain: --competitor X → [X]; else house + all
    active freestyle competitors."""
    if args.competitor and not args.all:
        return [args.competitor]
    from helm.competition.competitors import freestyle_competitors
    return [HOUSE_COMPETITOR_ID] + [c.id for c in freestyle_competitors()]


def _dry_run(agents: list[str]) -> int:
    """Print an APPROXIMATE lexical grouping for each agent. NO DB WRITES.

    This is a sizing hint only. The lexical Jaccard clusterer under-dedups
    real backlogs (restatements share too few tokens), so this preview is NOT
    the plan that will run — the PM decides the actual accept/supersede/reject
    SEMANTICALLY from the full proposal text at drain time. We show the top
    DRY_RUN_PREVIEW_CLUSTERS approximate groups plus the full backlog size so
    the operator can eyeball the shape before nudging for real.
    """
    print("[drain] DRY RUN — approximate lexical grouping below. Final dedup is "
          "decided SEMANTICALLY by the PM at drain time (this preview does NOT "
          "predict the actual accept/supersede plan).")
    for aid in agents:
        clusters = cluster_open_proposals(aid)
        total_open = open_proposal_count(aid)
        # How many groups are genuine multi-member lexical clusters vs lone
        # singletons the lexical metric couldn't merge (the usual case).
        multi = sum(1 for cl in clusters if cl.recurrence > 1)
        print(f"\n══ {aid} — {total_open} open proposal(s); "
              f"{len(clusters)} approx. lexical group(s) "
              f"({multi} multi-member, {len(clusters) - multi} singletons) ══")
        if not clusters:
            print("   (backlog empty — nothing to drain)")
            continue
        shown = clusters[:DRY_RUN_PREVIEW_CLUSTERS]
        for rank, cl in enumerate(shown, 1):
            rep = cl.representative
            others = [pid for pid in cl.member_ids
                      if pid != cl.representative_id]
            print(f"\n  [{rank}] approx. group score={cl.score} "
                  f"(recurrence={cl.recurrence} × confidence={cl.confidence})")
            print(f"      category: {cl.category}")
            print(f"      sample #{cl.representative_id}: "
                  f"{(rep.get('title') or '')[:90]}")
            if others:
                preview = ", ".join(f"#{p}" for p in others[:12])
                more = "" if len(others) <= 12 else f" (+{len(others) - 12} more)"
                print(f"      lexically-similar siblings: {preview}{more}")
            else:
                print("      (singleton — lexical metric found no sibling; the "
                      "PM may still supersede it semantically)")
        if len(clusters) > len(shown):
            print(f"\n  … {len(clusters) - len(shown)} more approx. group(s) not "
                  "shown.")
    print("\n[drain] dry-run only — no proposals, tasks, or releases changed. "
          "Run without --dry-run to let the PM consolidate the backlog "
          "semantically.")
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

        pm_result = run_backlog_drain(aid, max_clusters=ACCEPTS_PER_PASS)
        tasks = pm_result.get("tasks_created") or []
        accepted = pm_result.get("proposals_accepted") or []
        superseded = pm_result.get("proposals_superseded") or []
        rejected = pm_result.get("proposals_rejected") or []
        # `clusters_seen` is retained for back-compat; it now means "raw
        # proposals shown to the PM this batch".
        batch_seen = pm_result.get("proposals_seen",
                                   pm_result.get("clusters_seen", 0))
        print(f"[drain:{aid}] iter {i}: open={remaining} "
              f"batch={batch_seen} tasks={tasks} "
              f"accepted={len(accepted)} superseded={len(superseded)} "
              f"rejected={len(rejected)}")

        # If the PM made no progress (empty batch, no status moves), bail to
        # avoid spinning — either the backlog is empty or every proposal was
        # rejected as invalid. Re-check the count to be sure.
        moved = bool(tasks or accepted or superseded or rejected)
        if batch_seen == 0 and not moved:
            print(f"[drain:{aid}] no proposals left to decide — done")
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
                   help="Print an APPROXIMATE lexical grouping only (no LLM, no "
                        "DB writes, no agents); the real dedup is decided "
                        "semantically by the PM at drain time.")
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

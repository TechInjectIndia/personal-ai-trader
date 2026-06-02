"""
PM-only screening drain — the SAFE in-hours path to clear the open-proposal
backlog. Loops `run_backlog_drain` (Opus 4.8 screening) per agent until no
status='open' proposals remain, WITHOUT running the Engineer or Tester. Pure DB
writes: redundant restatements get superseded/rejected, the best distinct ideas
get accepted into queued agent_tasks that the Engineer builds (on Sonnet 4.6) in
its normal Sunday window.

Unlike scripts/drain_backlog.py this NEVER mutates the live code tree or runs
pytest, so it's safe to run while the market is open. One-off operator nudge.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from helm.agents.backlog import open_proposal_count  # noqa: E402
from helm.agents.pm import run_backlog_drain  # noqa: E402
from helm.competition.competitors import freestyle_competitors  # noqa: E402
from helm.config import HOUSE_COMPETITOR_ID  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
MAX_ITERS_PER_AGENT = 80


def drain_agent(aid: str) -> None:
    start = open_proposal_count(aid)
    print(f"\n== {aid}: {start} open ==", flush=True)
    for i in range(1, MAX_ITERS_PER_AGENT + 1):
        before = open_proposal_count(aid)
        if before == 0:
            print(f"[{aid}] empty after {i - 1} pass(es)", flush=True)
            return
        r = run_backlog_drain(aid)  # defaults to AGENT_MODEL = Opus 4.8
        after = open_proposal_count(aid)
        print(
            f"[{aid}] pass {i}: open {before}->{after} "
            f"seen={r.get('proposals_seen', r.get('clusters_seen', 0))} "
            f"accept={len(r.get('proposals_accepted') or [])} "
            f"supersede={len(r.get('proposals_superseded') or [])} "
            f"reject={len(r.get('proposals_rejected') or [])}",
            flush=True,
        )
        if after >= before:
            # No proposal left 'open' this pass — PM declined the whole batch as
            # invalid, or the batch couldn't be screened. Stop to avoid a costly
            # spin on Opus.
            print(f"[{aid}] no progress (open {before}->{after}) — stopping", flush=True)
            return
    print(f"[{aid}] hit {MAX_ITERS_PER_AGENT}-pass cap; {open_proposal_count(aid)} open left",
          flush=True)


def main() -> int:
    load_dotenv(ENV_PATH, override=False)
    agents = [HOUSE_COMPETITOR_ID] + [c.id for c in freestyle_competitors()]
    print(f"[drain-screen] agents: {agents}", flush=True)
    for aid in agents:
        drain_agent(aid)
    print("\n[drain-screen] done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

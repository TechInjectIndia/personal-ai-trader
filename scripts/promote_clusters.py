"""
Cluster promotion driver (Self-Improvement Loop v2, FRD G2 runtime) — the step
that turns ranked proposal clusters into shipped fixes.

Each run, per targeted surface:
  1. reconcile_cluster_statuses() — close in_flight clusters whose task verified
     (→ verified) or failed (→ reopened).
  2. push_escalation_alerts() — surface escalated, recurring, owner-less clusters
     to the human Action Center.
  3. run_cluster_promotion() — promote the top open cluster to one typed Engineer
     task and mark it in_flight (respects one-change-in-flight backpressure).

The existing Engineer/Tester crons then build + verify the task; the next run's
reconcile step flips the cluster to verified or reopens it. This closes the
loop: an escalated cluster (a freestyle-surfaced shared-code bug) now has a path
to ship without a human — the automated form of the manual cap-clamp fix.

  python scripts/promote_clusters.py                     # house surface, 1 promotion
  python scripts/promote_clusters.py --surface gemini-momentum
  python scripts/promote_clusters.py --all --max 1       # house + every freestyle
  python scripts/promote_clusters.py --reconcile-only    # just close/reopen + alerts

Shipped dark: run manually. Not on cron until the runtime is proven.
Exit codes: 0 ok, 1 unexpected error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from helm.agents.clustering import (  # noqa: E402
    push_escalation_alerts,
    reconcile_cluster_statuses,
)
from helm.agents.pm import run_cluster_promotion  # noqa: E402
from helm.agents.base import is_autonomy_paused  # noqa: E402
from helm.config import HOUSE_COMPETITOR_ID  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _surfaces(args) -> list[str]:
    if args.all:
        from helm.competition.competitors import freestyle_competitors
        return [HOUSE_COMPETITOR_ID] + [c.id for c in freestyle_competitors()]
    return [args.surface or HOUSE_COMPETITOR_ID]


def main() -> int:
    ap = argparse.ArgumentParser(description="Promote proposal clusters to tasks (G2).")
    ap.add_argument("--surface", help="one surface (house-claude or a competitor id)")
    ap.add_argument("--all", action="store_true", help="house + every freestyle agent")
    ap.add_argument("--max", type=int, default=1,
                    help="max promotions per surface this run (default 1)")
    ap.add_argument("--reconcile-only", action="store_true",
                    help="only close/reopen finished clusters + push alerts")
    ap.add_argument("--force", action="store_true",
                    help="ignore one-change-in-flight backpressure")
    args = ap.parse_args()

    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    if is_autonomy_paused():
        print("autonomy paused — no-op.")
        return 0

    # 1. reconcile finished clusters (global), 2. surface escalations.
    rec = reconcile_cluster_statuses()
    if rec["verified"] or rec["reopened"]:
        print(f"reconciled: verified={rec['verified']} reopened={rec['reopened']}")
    # 1b. G4: decay instincts whose agent kept losing after promotion (reopens
    # the source cluster so the loop re-fixes the lesson). Fail-safe.
    try:
        from helm.agents.instincts import run_decay_pass
        dec = run_decay_pass()
        if dec["decayed"] or dec["reopened"]:
            print(f"instincts: evaluated={dec['evaluated']} "
                  f"decayed={dec['decayed']} reopened={dec['reopened']}")
    except Exception as exc:  # noqa: BLE001
        print(f"instinct decay pass skipped: {str(exc)[:120]}")
    n_alerts = push_escalation_alerts()
    if n_alerts:
        print(f"escalation alerts pushed to Action Center: {n_alerts}")

    if args.reconcile_only:
        return 0

    # 3. promote top clusters per surface.
    for surface in _surfaces(args):
        for _ in range(max(1, args.max)):
            res = run_cluster_promotion(surface, force=args.force)
            if res.get("deferred"):
                print(f"[{surface}] {res['reason']}")
                break
            print(f"[{surface}] promoted cluster {res['cluster_id']} "
                  f"→ task {res['task_created']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(1) from None

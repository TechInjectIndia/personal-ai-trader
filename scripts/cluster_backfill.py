"""
Cluster backfill (FRD G1) — collapse the existing open-proposal backlog into
`proposal_clusters`.

The retro pipeline left ~1,700 status='open' improvement_proposals, mostly
restatements of a few dozen distinct ideas (the cap-clamp fix recurred ~90×).
This script assigns each un-clustered open proposal to a cluster (matching an
existing one by meaning, or opening a new one) via
`helm.agents.clustering.assign_and_persist`.

It is BOUNDED and RESUMABLE: each run processes at most `--limit` proposals and
only ever touches `cluster_id IS NULL` rows, so a human can drain the backlog in
chunks (each proposal is one cheap LLM call) and just re-run until done.

Proposals are processed OLDEST-first so the earliest phrasing of an idea
establishes the cluster and later restatements match onto it.

  python scripts/cluster_backfill.py --dry-run            # counts only, no LLM
  python scripts/cluster_backfill.py --limit 50           # one chunk
  python scripts/cluster_backfill.py --competitor gemini-momentum --limit 100
  python scripts/cluster_backfill.py --all --limit 2000   # drain everything

Exit codes: 0 ok (or nothing to do / dry-run), 1 on an unexpected error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from helm.agents.clustering import assign_and_persist  # noqa: E402
from helm.data.store import conn  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _unclustered(competitor_id: str | None, limit: int) -> list[dict]:
    """Open proposals not yet in a cluster, oldest-first, optionally one agent."""
    sql = ("SELECT id, COALESCE(competitor_id, 'house-claude') AS cid, title "
           "FROM improvement_proposals "
           "WHERE status = 'open' AND cluster_id IS NULL")
    args: list = []
    if competitor_id is not None:
        # house = (NULL OR 'house-claude'); freestyle = exact match.
        if competitor_id in ("house", "house-claude"):
            sql += " AND (competitor_id IS NULL OR competitor_id = 'house-claude')"
        else:
            sql += " AND competitor_id = %s"
            args.append(competitor_id)
    sql += " ORDER BY created_ts ASC LIMIT %s"
    args.append(limit)
    with conn() as c:
        return list(c.execute(sql, tuple(args)))


def _counts() -> list[dict]:
    with conn() as c:
        return list(c.execute(
            "SELECT COALESCE(competitor_id, 'house-claude') AS cid, "
            "COUNT(*) FILTER (WHERE cluster_id IS NULL) AS unclustered, "
            "COUNT(*) AS open_total "
            "FROM improvement_proposals WHERE status = 'open' "
            "GROUP BY 1 ORDER BY unclustered DESC"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill proposal clusters (G1).")
    ap.add_argument("--competitor", help="one agent id (e.g. gemini-momentum, "
                    "house-claude); default = all agents")
    ap.add_argument("--all", action="store_true", help="process every agent")
    ap.add_argument("--limit", type=int, default=50,
                    help="max proposals this run (each = one LLM call)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print unclustered counts per agent; no LLM, no writes")
    args = ap.parse_args()

    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    if args.dry_run:
        print("Unclustered open proposals by agent (no writes):")
        total = 0
        for r in _counts():
            total += int(r["unclustered"])
            print(f"  {r['cid']:20} unclustered={r['unclustered']:5}  "
                  f"open_total={r['open_total']}")
        print(f"  {'TOTAL':20} unclustered={total}")
        return 0

    competitor = None if args.all else args.competitor
    todo = _unclustered(competitor, args.limit)
    if not todo:
        print("nothing to cluster (no unclustered open proposals match).")
        return 0

    print(f"clustering {len(todo)} proposal(s)"
          + (f" for {competitor}" if competitor else " across all agents") + " …")
    new_clusters = 0
    matched = 0
    errors = 0
    for i, p in enumerate(todo, 1):
        try:
            res = assign_and_persist(int(p["id"]))
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort the run
            errors += 1
            print(f"  [{i}/{len(todo)}] #{p['id']} ERROR: {str(exc)[:120]}")
            continue
        if res.get("created_new"):
            new_clusters += 1
            tag = "NEW" + (" ↑house" if res.get("escalated") else "")
        else:
            matched += 1
            tag = "match"
        print(f"  [{i}/{len(todo)}] #{p['id']} [{p['cid']}] → cluster "
              f"{res['cluster_id']} ({tag})")

    print(f"\ndone: {matched} matched, {new_clusters} new clusters, {errors} errors.")
    print("re-run to continue (resumable); `--dry-run` to see what remains.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted; progress is saved — re-run to continue.")
        raise SystemExit(1) from None

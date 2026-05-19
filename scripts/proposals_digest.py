"""
Improvement-proposals digest — surfaces what the retros are telling us to fix.

Each retrospective produces 0–3 concrete change proposals (see helm.retro
SYSTEM_PROMPT). This script aggregates them by category, ranks by frequency
× confidence so themes that keep recurring across trades rise to the top,
and prints a markdown digest the human (or Claude) can act on.

Workflow:
  1. Run after a few trading days to see the top open proposals.
  2. Decide which ones to apply; mark accepted/applied via --apply/--reject.
  3. Applied proposals stop showing in the default 'open' view.

Usage:
  python scripts/proposals_digest.py                 # open proposals, last 30 days
  python scripts/proposals_digest.py --days 7
  python scripts/proposals_digest.py --category decider_prompt
  python scripts/proposals_digest.py --status all
  python scripts/proposals_digest.py --apply 42 --note "shipped in commit abc"
  python scripts/proposals_digest.py --reject 17 --note "addressed by #42"
  python scripts/proposals_digest.py --show 42       # full body of one proposal
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.data.store import conn

CATEGORY_HEADERS = {
    "strategy":       "Strategy rule changes",
    "decider_prompt": "Decider prompt tweaks",
    "risk":           "Risk-gate changes",
    "sizing":         "Position sizing",
    "execution":      "Execution / trade management",
    "data":           "Data into the decider",
    "meta":           "Process / monitoring",
}


def _fetch(category: str | None, status: str, days: int) -> list[dict]:
    clauses = ["p.created_ts >= now() - make_interval(days => %s)"]
    args: list = [days]
    if status != "all":
        clauses.append("p.status = %s")
        args.append(status)
    if category:
        clauses.append("p.category = %s")
        args.append(category)
    sql = (
        "SELECT p.id, p.retro_id, p.category, p.title, p.rationale, "
        "       p.proposed_change, p.evidence, p.confidence, p.status, "
        "       p.created_ts, "
        "       r.kind, r.trade_id, r.decision_id, r.verdict_label "
        "FROM improvement_proposals p "
        "JOIN trade_retrospectives r ON r.id = p.retro_id "
        "WHERE " + " AND ".join(clauses) + " "
        "ORDER BY p.created_ts DESC"
    )
    with conn() as c:
        return list(c.execute(sql, tuple(args)))


def _cluster_key(title: str) -> str:
    """Loose dedupe: lowercase, strip punctuation/trailing parens, collapse spaces."""
    t = title.lower()
    for ch in "().,:;-—":
        t = t.replace(ch, " ")
    return " ".join(t.split())


def _show_digest(rows: list[dict]) -> None:
    if not rows:
        print("(no proposals in window)")
        return

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    print(f"# Improvement proposals digest — {len(rows)} item(s)\n")

    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        items = by_cat[cat]
        print(f"## {CATEGORY_HEADERS.get(cat, cat)} ({len(items)})\n")

        # Cluster within category by fuzzy title match. A theme that recurs
        # gets surfaced once with its highest-confidence instance + a count.
        clusters: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            clusters[_cluster_key(it["title"])].append(it)

        ranked = sorted(
            clusters.values(),
            key=lambda lst: (-len(lst), -max(x["confidence"] or 0 for x in lst)),
        )
        for group in ranked:
            best = max(group, key=lambda x: (x["confidence"] or 0, x["created_ts"]))
            n = len(group)
            n_tag = f" ×{n}" if n > 1 else ""
            ids = ",".join(str(x["id"]) for x in group)
            src = ", ".join(
                f"{x['kind'].lower()}#{x['trade_id'] or x['decision_id']}"
                for x in group
            )
            print(f"### [{best['confidence']}/5]{n_tag} {best['title']}")
            print(f"  *id={ids} · status={best['status']} · from {src}*\n")
            print(f"  **Why:** {best['rationale']}\n")
            print(f"  **Change:** {best['proposed_change']}\n")
            if best["evidence"]:
                print(f"  **Evidence:** `{best['evidence']}`\n")


def _show_one(pid: int) -> int:
    with conn() as c:
        row = c.execute(
            "SELECT p.*, r.kind, r.trade_id, r.decision_id "
            "FROM improvement_proposals p "
            "JOIN trade_retrospectives r ON r.id = p.retro_id "
            "WHERE p.id = %s",
            (pid,),
        ).fetchone()
    if not row:
        print(f"no proposal id {pid}", file=sys.stderr)
        return 1
    src = f"{row['kind'].lower()}#{row['trade_id'] or row['decision_id']}"
    print(f"# Proposal {pid} — {row['category']} · {row['status']}")
    print(f"_confidence {row['confidence']}/5 · from {src} · "
          f"created {row['created_ts']:%Y-%m-%d %H:%M}_\n")
    print(f"## {row['title']}\n")
    print(f"**Why:** {row['rationale']}\n")
    print(f"**Change:** {row['proposed_change']}\n")
    if row["evidence"]:
        print(f"**Evidence:** `{row['evidence']}`\n")
    if row["status_note"]:
        print(f"**Status note:** {row['status_note']}")
    return 0


def _set_status(pid: int, new_status: str, note: str | None) -> int:
    with conn() as c:
        row = c.execute(
            "UPDATE improvement_proposals "
            "SET status = %s, status_note = %s, status_ts = now() "
            "WHERE id = %s RETURNING id, title, category",
            (new_status, note, pid),
        ).fetchone()
    if not row:
        print(f"no proposal id {pid}", file=sys.stderr)
        return 1
    print(f"proposal {row['id']} ({row['category']}) → {new_status}: {row['title']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30,
                   help="Look-back window in days (default 30)")
    p.add_argument("--category", choices=tuple(CATEGORY_HEADERS),
                   help="Restrict to one category")
    p.add_argument("--status",
                   choices=("open", "accepted", "rejected", "applied",
                            "superseded", "all"),
                   default="open",
                   help="Default 'open' — applied / rejected are hidden")
    p.add_argument("--show", type=int, metavar="ID",
                   help="Print full body of one proposal and exit")
    p.add_argument("--apply", type=int, metavar="ID",
                   help="Mark proposal as applied (shipped)")
    p.add_argument("--accept", type=int, metavar="ID",
                   help="Mark proposal as accepted (queued to ship)")
    p.add_argument("--reject", type=int, metavar="ID",
                   help="Mark proposal as rejected")
    p.add_argument("--supersede", type=int, metavar="ID",
                   help="Mark proposal as superseded by a newer one")
    p.add_argument("--note", default=None,
                   help="Free-text note attached to the status change")
    args = p.parse_args()

    if args.show is not None:
        return _show_one(args.show)
    for new_status, pid in (("applied", args.apply), ("accepted", args.accept),
                            ("rejected", args.reject),
                            ("superseded", args.supersede)):
        if pid is not None:
            return _set_status(pid, new_status, args.note)

    rows = _fetch(args.category, args.status, args.days)
    _show_digest(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

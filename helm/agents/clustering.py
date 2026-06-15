"""
Proposal clustering — the converging apply-arm's first stage (FRD G1/G2).

The retro pipeline emits hundreds of near-duplicate `improvement_proposals`:
the same idea restated from many trades. A flat backlog of 1,700+ open rows is
unrankable — the loop can't tell "proposed once" from "proposed 90×". This
module collapses proposals into `proposal_clusters` (one row per DISTINCT idea)
so recurrence = member count and the digest ranks ideas, not restatements.

Two responsibilities, kept separate so the math is unit-testable without a DB:

  * PURE math — `decay_confidence`, `cluster_rank_score`, `category_to_layer`,
    `classify_surface` (G2 routing). No DB, no LLM, deterministic.
  * DB store + LLM assignment — `open_clusters`, `create_cluster`,
    `recompute_cluster`, `assign_and_persist`. The semantic match (does this
    proposal restate an existing cluster?) is the LLM's job, exactly like the
    backlog drain — lexical overlap is known-useless here (see
    `helm/agents/backlog.py`). The candidate set is bounded to the proposal's
    own target_surface, so the prompt stays small and O(new), not O(backlog).

Confidence is normalised to 0–1 (proposal confidence is 1–5) and decays toward
0 as a cluster goes stale, so an idea nobody has re-surfaced in weeks naturally
sinks beneath fresh recurring themes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from helm.config import AGENT_MODEL_DEFAULT, HOUSE_COMPETITOR_ID
from helm.data.store import conn, insert_audit
from helm.llm import complete_json

# Decay factor per week of staleness applied to a cluster's confidence. 0.95/wk
# ≈ half-life of ~13.5 weeks: slow enough that a real recurring theme stays hot,
# fast enough that a one-off from two months ago stops outranking fresh work.
CONFIDENCE_DECAY_PER_WEEK = 0.95

# Map a proposal's `category` to a cluster `layer`. The only rename is
# decider_prompt→decider; 'persona' is reserved for freestyle persona edits and
# is assigned by surface classification, not category.
_CATEGORY_TO_LAYER = {
    "strategy": "strategy",
    "decider_prompt": "decider",
    "risk": "risk",
    "sizing": "sizing",
    "execution": "execution",
    "data": "data",
    "meta": "meta",
}

# Substrings that mean "the fix lives in shared house code" — a freestyle agent
# naming any of these is diagnosing a bug outside its persona/config surface, so
# the cluster must escalate to the house owner (G2). File paths first, then the
# phrases the retro LLM actually uses ("out of freestyle surface", etc.).
_SHARED_CODE_MARKERS = (
    "helm/orchestrator", "scripts/paper_execute", "helm/wallet", "helm/config",
    "helm/competition/execute", "risk.evaluate", "risk gate", "risk-gate",
    "position sizing", "position-sizing", "sizer", "notional cap", "per-trade cap",
    "shared house code", "shared code", "house code", "out of freestyle surface",
    "out-of-surface", "out of surface", "shared sizing", "execution layer",
)


# ─── pure math (no DB / no LLM) ──────────────────────────────────────────


def normalise_confidence(raw: int | None) -> Decimal:
    """Proposal confidence is 1–5 (or None). Normalise to 0–1 for the cluster."""
    if raw is None:
        return Decimal("0.6")  # neutral prior == 3/5
    c = max(1, min(5, int(raw)))
    return (Decimal(c) / Decimal(5)).quantize(Decimal("0.01"))


def decay_confidence(max_member_conf: Decimal, weeks_stale: float) -> Decimal:
    """Cluster confidence = newest-member confidence decayed by staleness.

    `max_member_conf` is already normalised 0–1. `weeks_stale` ≥ 0. Pure.
    """
    weeks = max(0.0, float(weeks_stale))
    factor = Decimal(str(CONFIDENCE_DECAY_PER_WEEK ** weeks))
    return (Decimal(max_member_conf) * factor).quantize(Decimal("0.01"))


def cluster_rank_score(recurrence: int, confidence: Decimal | float,
                       economic_priority: Decimal | float = 1) -> float:
    """Ranking key for the digest: recurrence × confidence × economic_priority.

    Recurrence is the dominant term (the PM principle "recurrence beats
    novelty"); confidence and economic weight break ties. Pure.
    """
    return float(max(0, recurrence)) * float(confidence) * float(economic_priority)


def category_to_layer(category: str | None) -> str:
    """Cluster layer for a proposal category. Unknown → 'meta' (never crashes)."""
    return _CATEGORY_TO_LAYER.get((category or "").strip(), "meta")


def classify_surface(competitor_id: str | None, *texts: str) -> tuple[str, bool, str | None]:
    """G2 routing: who owns the fix for this proposal?

    Returns (target_surface, escalated, origin_competitor_id).
      * house proposal              → ('house', False, None)
      * freestyle, in-surface       → (competitor_id, False, None)
      * freestyle naming shared code → ('house', True, competitor_id)  # escalate

    `texts` are the proposal's title/proposed_change/rationale; the shared-code
    markers are matched case-insensitively against their concatenation. Pure.
    """
    if competitor_id is None or competitor_id == HOUSE_COMPETITOR_ID:
        return "house", False, None
    blob = " ".join(t for t in texts if t).lower()
    if any(m in blob for m in _SHARED_CODE_MARKERS):
        return "house", True, competitor_id
    return competitor_id, False, None


# ─── DB store ────────────────────────────────────────────────────────────


def _weeks_stale(last_member_ts: datetime) -> float:
    now = datetime.now(timezone.utc)
    ts = last_member_ts if last_member_ts.tzinfo else last_member_ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds() / (7 * 86400))


def open_clusters(target_surface: str | None = None) -> list[dict]:
    """Open clusters, optionally scoped to one surface (for the assign prompt)."""
    sql = ("SELECT id, theme, layer, target_surface, escalated, confidence, "
           "recurrence, status FROM proposal_clusters WHERE status = 'open'")
    args: tuple = ()
    if target_surface is not None:
        sql += " AND target_surface = %s"
        args = (target_surface,)
    sql += " ORDER BY recurrence DESC, confidence DESC"
    with conn() as c:
        return list(c.execute(sql, args))


def create_cluster(*, theme: str, layer: str, target_surface: str,
                   escalated: bool, origin_competitor_id: str | None,
                   confidence: Decimal, representative_proposal_id: int | None) -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO proposal_clusters
                (theme, layer, target_surface, escalated, origin_competitor_id,
                 confidence, recurrence, representative_proposal_id, last_member_ts)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s, now())
            RETURNING id
            """,
            (theme[:300], layer, target_surface, escalated, origin_competitor_id,
             confidence, representative_proposal_id),
        ).fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return int(row["id"])


def recompute_cluster(cluster_id: int) -> dict | None:
    """Recompute a cluster's aggregates from its current members.

    recurrence = member count; confidence = newest-member normalised confidence
    decayed by weeks since that member; representative = highest-confidence
    member; last_member_ts = newest member's created_ts. Idempotent.
    """
    with conn() as c:
        members = list(c.execute(
            "SELECT id, confidence, created_ts FROM improvement_proposals "
            "WHERE cluster_id = %s ORDER BY created_ts DESC",
            (cluster_id,),
        ))
        if not members:
            c.execute("UPDATE proposal_clusters SET recurrence = 0, "
                      "confidence = 0, updated_ts = now() WHERE id = %s",
                      (cluster_id,))
            return None
        newest = members[0]
        last_ts = newest["created_ts"]
        max_norm = max(normalise_confidence(m["confidence"]) for m in members)
        conf = decay_confidence(max_norm, _weeks_stale(last_ts))
        rep = max(members, key=lambda m: normalise_confidence(m["confidence"]))
        c.execute(
            "UPDATE proposal_clusters SET recurrence = %s, confidence = %s, "
            "last_member_ts = %s, representative_proposal_id = %s, "
            "updated_ts = now() WHERE id = %s",
            (len(members), conf, last_ts, int(rep["id"]), cluster_id),
        )
    return {"cluster_id": cluster_id, "recurrence": len(members), "confidence": conf}


def attach_proposal(proposal_id: int, cluster_id: int) -> None:
    """Point a proposal at its cluster, then refresh the cluster's aggregates."""
    with conn() as c:
        c.execute("UPDATE improvement_proposals SET cluster_id = %s WHERE id = %s",
                  (cluster_id, proposal_id))
    recompute_cluster(cluster_id)


def _load_proposal(proposal_id: int) -> dict | None:
    with conn() as c:
        return c.execute(
            "SELECT id, category, title, rationale, proposed_change, confidence, "
            "competitor_id, cluster_id FROM improvement_proposals WHERE id = %s",
            (proposal_id,),
        ).fetchone()


# ─── LLM assignment ──────────────────────────────────────────────────────

ASSIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The id of an existing same-surface cluster this proposal restates, or
        # null to open a new cluster.
        "match_cluster_id": {"type": ["integer", "null"]},
        # Required when match_cluster_id is null: a canonical one-line theme.
        "new_theme": {"type": ["string", "null"]},
    },
    "required": ["match_cluster_id"],
}

ASSIGN_SYSTEM = """You deduplicate trading-bot improvement proposals by MEANING.

You are given ONE new proposal and a list of EXISTING idea-clusters (each with an
id and a one-line theme). Decide whether the new proposal restates one of the
existing clusters or is a genuinely DISTINCT idea.

Match by meaning, not words: "enforce R:R floor", "reward-to-risk hard gate", and
"reward-vs-risk minimum before entry" are the SAME cluster though they share no
tokens. Only open a new cluster when no existing theme captures the idea.

Output strict JSON:
  {"match_cluster_id": <id of the cluster it restates>}        # if it matches
  {"match_cluster_id": null, "new_theme": "<canonical one-line idea>"}  # if new

The new_theme must be a crisp, generic statement of the idea (not this trade's
specifics) so future restatements cluster onto it. No markdown, no prose."""


def _build_assign_user(proposal: dict, candidates: list[dict]) -> str:
    payload = {
        "new_proposal": {
            "title": proposal.get("title"),
            "proposed_change": proposal.get("proposed_change"),
            "rationale": proposal.get("rationale"),
            "category": proposal.get("category"),
        },
        "existing_clusters": [
            {"id": int(c["id"]), "theme": c["theme"]} for c in candidates
        ],
    }
    return ("Match this proposal to an existing cluster by meaning, or open a "
            "new one.\n\n```json\n" + json.dumps(payload, indent=2, default=str)
            + "\n```")


def assign_and_persist(
    proposal_id: int, *,
    model: str | None = None,
    mode: str | None = None,
    llm: Callable[..., dict] = complete_json,
) -> dict:
    """Assign one proposal to a cluster (existing or new) and persist it.

    Deterministic G2 surface classification runs FIRST, so the LLM only ever
    chooses among clusters on the same surface (bounded, cheap). If there are no
    same-surface candidates, no LLM call is made — a new cluster is opened
    directly. `llm` is injectable for tests. Idempotent: an already-clustered
    proposal is returned untouched.

    Returns {proposal_id, cluster_id, created_new, target_surface, escalated}.
    """
    model = model or os.environ.get("AGENT_MODEL", AGENT_MODEL_DEFAULT)
    mode = (mode or os.environ.get("LLM_MODE", "cli")).strip().lower()

    p = _load_proposal(proposal_id)
    if p is None:
        raise ValueError(f"proposal {proposal_id} not found")
    if p.get("cluster_id"):
        return {"proposal_id": proposal_id, "cluster_id": int(p["cluster_id"]),
                "created_new": False, "skipped": "already_clustered"}

    surface, escalated, origin = classify_surface(
        p.get("competitor_id"), p.get("title") or "",
        p.get("proposed_change") or "", p.get("rationale") or "")
    layer = category_to_layer(p.get("category"))
    conf = normalise_confidence(p.get("confidence"))
    candidates = open_clusters(target_surface=surface)

    def _new_cluster(theme: str) -> dict:
        cid = create_cluster(
            theme=theme, layer=layer, target_surface=surface, escalated=escalated,
            origin_competitor_id=origin, confidence=conf,
            representative_proposal_id=proposal_id)
        attach_proposal(proposal_id, cid)
        if escalated:
            insert_audit("agents", "cluster_escalated",
                         {"cluster_id": cid, "proposal_id": proposal_id,
                          "origin": origin, "theme": theme[:200]})
        return {"proposal_id": proposal_id, "cluster_id": cid, "created_new": True,
                "target_surface": surface, "escalated": escalated}

    if not candidates:
        return _new_cluster((p.get("title") or "untitled")[:300])

    parsed = llm(ASSIGN_SYSTEM, _build_assign_user(p, candidates),
                 schema=ASSIGN_SCHEMA, model=model, mode=mode,
                 max_tokens=400, temperature=0.0)
    match = parsed.get("match_cluster_id")
    valid_ids = {int(c["id"]) for c in candidates}
    if isinstance(match, int) and not isinstance(match, bool) and match in valid_ids:
        attach_proposal(proposal_id, match)
        return {"proposal_id": proposal_id, "cluster_id": match, "created_new": False,
                "target_surface": surface, "escalated": escalated}

    theme = str(parsed.get("new_theme") or p.get("title") or "untitled").strip()
    return _new_cluster(theme[:300])


def rank_open_clusters(limit: int = 40, target_surface: str | None = None) -> list[dict]:
    """Open clusters ranked by recurrence × confidence × economic_priority.

    The digest's headline view: distinct ideas, hottest first. Ranking is done
    in SQL for cheapness; `cluster_rank_score` is the same formula in pure form
    for the unit tests.
    """
    sql = (
        "SELECT id, theme, layer, target_surface, escalated, origin_competitor_id, "
        "confidence, recurrence, economic_priority, representative_proposal_id, "
        "(recurrence * confidence * economic_priority) AS rank_score "
        "FROM proposal_clusters WHERE status = 'open'"
    )
    args: list = []
    if target_surface is not None:
        sql += " AND target_surface = %s"
        args.append(target_surface)
    sql += " ORDER BY rank_score DESC, recurrence DESC LIMIT %s"
    args.append(int(limit))
    with conn() as c:
        return list(c.execute(sql, tuple(args)))


# ─── G2 runtime: selection, status, escalation surfacing ─────────────────

_VALID_CLUSTER_STATUS = ("open", "accepted", "in_flight", "verified",
                         "rejected", "superseded")


def set_cluster_status(cluster_id: int, status: str, note: str | None = None) -> bool:
    """Move a cluster through its lifecycle (open→accepted→in_flight→verified,
    or rejected/superseded). Returns True if a row was updated. The note is
    audited, not stored (the clusters table has no status_note column)."""
    if status not in _VALID_CLUSTER_STATUS:
        raise ValueError(f"invalid cluster status {status!r}")
    with conn() as c:
        row = c.execute(
            "UPDATE proposal_clusters SET status = %s, updated_ts = now() "
            "WHERE id = %s RETURNING id, target_surface, theme",
            (status, cluster_id),
        ).fetchone()
    if row is None:
        return False
    insert_audit("agents", "cluster_status_change",
                 {"cluster_id": cluster_id, "status": status,
                  "surface": row["target_surface"], "note": (note or "")[:200]})
    return True


def next_cluster_for_surface(target_surface: str) -> dict | None:
    """The single highest-ranked OPEN cluster a given surface should work next,
    with its representative proposal's body attached so the PM/engineer can turn
    it into a typed task. Escalated clusters are preferred (they encode insight
    that's been structurally stuck). Returns None if the surface is clear."""
    with conn() as c:
        row = c.execute(
            """
            SELECT pc.id, pc.theme, pc.layer, pc.target_surface, pc.escalated,
                   pc.origin_competitor_id, pc.recurrence, pc.confidence,
                   pc.economic_priority, pc.representative_proposal_id,
                   p.title AS rep_title, p.rationale AS rep_rationale,
                   p.proposed_change AS rep_proposed_change, p.category AS rep_category
            FROM proposal_clusters pc
            LEFT JOIN improvement_proposals p
                   ON p.id = pc.representative_proposal_id
            WHERE pc.status = 'open' AND pc.target_surface = %s
            ORDER BY pc.escalated DESC,
                     (pc.recurrence * pc.confidence * pc.economic_priority) DESC,
                     pc.recurrence DESC
            LIMIT 1
            """,
            (target_surface,),
        ).fetchone()
    return dict(row) if row is not None else None


def escalation_alerts(min_recurrence: int | None = None) -> list[dict]:
    """Open clusters escalated to the house surface (G2) that have recurred at
    least `min_recurrence` times — insight surfaced from a freestyle agent that
    needs a house owner. Defaults to config.ESCALATE_RECURRENCE."""
    from helm.config import ESCALATE_RECURRENCE
    threshold = ESCALATE_RECURRENCE if min_recurrence is None else min_recurrence
    with conn() as c:
        return list(c.execute(
            "SELECT id, theme, origin_competitor_id, recurrence, confidence "
            "FROM proposal_clusters "
            "WHERE status = 'open' AND escalated = true AND recurrence >= %s "
            "ORDER BY recurrence DESC, confidence DESC",
            (threshold,),
        ))


def push_escalation_alerts() -> int:
    """Surface escalated, recurring, owner-less clusters to the human Action
    Center (the visible form of structurally-stuck insight — the cap-bug failure
    mode). One queue item per cluster, keyed so re-runs replace not duplicate.
    Fail-safe: never raises. Returns the number of alerts pushed."""
    try:
        alerts = escalation_alerts()
    except Exception:  # noqa: BLE001
        return 0
    pushed = 0
    for a in alerts:
        try:
            from helm.dashboard.attention import enqueue
            enqueue(
                key=f"cluster_escalated_{a['id']}",
                title=f"Escalated fix needs a house owner (×{a['recurrence']})",
                detail=(f"{a['theme']} — surfaced from "
                        f"{a['origin_competitor_id']}, recurred "
                        f"{a['recurrence']}×. The freestyle agent can't touch "
                        f"shared house code; route it to the house engineer."),
                level="warn",
                where="Self-Improvement → proposal clusters",
                steps=["python scripts/proposals_digest.py --clusters",
                       f"Queue a house task for cluster {a['id']}"],
                actor="clustering",
            )
            pushed += 1
        except Exception:  # noqa: BLE001 — surfacing is best-effort
            continue
    return pushed


def reconcile_cluster_statuses() -> dict:
    """Close the loop for in_flight clusters by their task's outcome.

    Linkage is task.proposal_id → improvement_proposals.cluster_id (the promoted
    task carries the cluster's representative proposal id). For each in_flight
    cluster, inspect its linked task(s) + release(s):

      * a task that is 'done' AND either applied a config version (no release) or
        produced a VERIFIED release → cluster 'verified' + members 'applied'.
      * all linked tasks terminal-failed (failed/cancelled) or their release was
        reverted, none still pending → cluster reopened 'open' (insight survives;
        it'll be re-promoted later).
      * otherwise (still building / awaiting Tester) → left in_flight.

    Returns {verified: [...], reopened: [...]}.
    """
    verified: list[int] = []
    reopened: list[int] = []
    with conn() as c:
        in_flight = list(c.execute(
            "SELECT id FROM proposal_clusters WHERE status = 'in_flight'"))
        for clu in in_flight:
            cid = int(clu["id"])
            rows = list(c.execute(
                """
                SELECT t.status AS tstatus, t.release_id, r.status AS rstatus
                FROM agent_tasks t
                LEFT JOIN releases r ON r.id = t.release_id
                WHERE t.proposal_id IN (
                    SELECT id FROM improvement_proposals WHERE cluster_id = %s)
                """,
                (cid,),
            ))
            if not rows:
                continue
            states = []
            for t in rows:
                if t["tstatus"] == "done" and (
                        t["release_id"] is None or t["rstatus"] == "verified"):
                    states.append("ok")
                elif t["tstatus"] in ("failed", "cancelled") or t["rstatus"] == "reverted":
                    states.append("fail")
                else:
                    states.append("pending")
            if "ok" in states:
                verified.append(cid)
            elif states and all(s == "fail" for s in states):
                reopened.append(cid)
    # status writes outside the read loop (set_cluster_status opens its own conn)
    for cid in verified:
        set_cluster_status(cid, "verified", note="task verified")
        with conn() as c:
            c.execute("UPDATE improvement_proposals SET status = 'applied', "
                      "status_note = 'cluster verified', status_ts = now() "
                      "WHERE cluster_id = %s AND status = 'open'", (cid,))
    for cid in reopened:
        set_cluster_status(cid, "open", note="task failed/reverted — reopened")
    return {"verified": verified, "reopened": reopened}


__all__ = [
    "normalise_confidence", "decay_confidence", "cluster_rank_score",
    "category_to_layer", "classify_surface",
    "open_clusters", "create_cluster", "recompute_cluster", "attach_proposal",
    "assign_and_persist", "rank_open_clusters",
    "set_cluster_status", "next_cluster_for_surface", "escalation_alerts",
    "push_escalation_alerts", "reconcile_cluster_statuses",
]

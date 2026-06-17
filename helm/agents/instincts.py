"""G4 — the instinct ledger: durable, self-correcting per-agent memory.

When a proposal cluster is VERIFIED (its fix shipped and passed the eval-gate),
the lesson stops being a transient DB row and becomes a durable *instinct* the
owning agent is now applying — recorded in `agent_instincts`. This closes the
gap the 2026-06-15 deep dive found: confirmed learnings used to evaporate as
closed rows, so the loop showed no learning curve.

Two public operations:

  * ``promote_cluster_to_instinct(cluster_id)`` — called when a cluster flips to
    'verified' (from ``reconcile_cluster_statuses``). Idempotent upsert keyed
    (competitor_id, cluster_id).

  * ``run_decay_pass()`` — called on the daily post-close cadence. For each
    active instinct, looks at the owning agent's trades closed *since the
    instinct was promoted* (or last evaluated). If the agent kept losing, the
    instinct clearly isn't holding: bump its miss count and decay its
    confidence; once below the floor, mark it 'decayed' and REOPEN its source
    cluster so the loop re-fixes it. Self-correcting memory.

Attribution is deliberately book-level, not per-instinct: we can't cleanly
attribute one trade to one free-text lesson, so "the agent kept losing after
learning X" is the honest, computable proxy for "later trades contradict X"
(the PRD's decay trigger). Documented as an approximation on purpose.

No LLM calls — pure SQL + arithmetic, safe to run every cadence tick.
"""

from __future__ import annotations

from decimal import Decimal

from helm.config import (
    HOUSE_COMPETITOR_ID,
    HOUSE_TRADE_FILTER,
    INSTINCT_CONFIDENCE_FLOOR,
    INSTINCT_DECAY_FACTOR,
)
from helm.data.store import conn, insert_audit

# Map a cluster's layer to the durable artifact the verified fix lives in, so the
# ledger can show *where* each learning is encoded.
_LAYER_TO_ARTIFACT = {
    "persona": "persona",
    "decider": "decider_prompt",
    "strategy": "strategy_config",
    "sizing": "config",
    "risk": "config",
    "execution": "config",
    "data": "config",
    "meta": "config",
}

# Only decay on real evidence: require at least this many newly-closed trades
# since the last evaluation before we judge an instinct.
_MIN_TRADES_TO_JUDGE = 5


def _book_filter(competitor_id: str) -> tuple[str, tuple]:
    """SQL fragment selecting one agent's paper_trades. House is (NULL OR
    'house-claude'); the cluster surface 'house' maps to the house book too."""
    if competitor_id in ("house", HOUSE_COMPETITOR_ID):
        return HOUSE_TRADE_FILTER, ()
    return "competitor_id = %s", (competitor_id,)


def promote_cluster_to_instinct(cluster_id: int) -> dict | None:
    """Promote a verified cluster into a durable per-agent instinct (idempotent).

    Returns the instinct row dict, or None if the cluster is missing. Keeps the
    original ``promoted_ts`` on re-promotion so the decay window is stable."""
    with conn() as c:
        clu = c.execute(
            "SELECT id, theme, layer, target_surface, confidence "
            "FROM proposal_clusters WHERE id = %s",
            (cluster_id,),
        ).fetchone()
        if clu is None:
            return None
        surface = clu["target_surface"] or "house"
        artifact = _LAYER_TO_ARTIFACT.get(clu["layer"], "config")
        conf = float(clu["confidence"]) if clu["confidence"] is not None else 1.0
        conf = max(INSTINCT_CONFIDENCE_FLOOR, min(1.0, conf if conf > 0 else 1.0))
        row = c.execute(
            """
            INSERT INTO agent_instincts
                (competitor_id, cluster_id, statement, layer, artifact_kind,
                 confidence, status, promoted_ts, updated_ts)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', now(), now())
            ON CONFLICT (competitor_id, cluster_id) DO UPDATE SET
                statement = EXCLUDED.statement,
                layer = EXCLUDED.layer,
                artifact_kind = EXCLUDED.artifact_kind,
                -- re-promotion of a previously-decayed instinct revives it
                status = 'active',
                confidence = GREATEST(agent_instincts.confidence, EXCLUDED.confidence),
                updated_ts = now()
            RETURNING id, competitor_id, cluster_id, confidence, status
            """,
            (surface, cluster_id, (clu["theme"] or "")[:1000], clu["layer"],
             artifact, conf),
        ).fetchone()
    insert_audit("agents", "instinct_promoted",
                 {"cluster_id": cluster_id, "competitor_id": row["competitor_id"],
                  "artifact": artifact})
    return dict(row)


def _trades_since(c, competitor_id: str, since) -> tuple[int, Decimal]:
    """(#closed trades, net P&L) for an agent on trades closed after ``since``."""
    where, params = _book_filter(competitor_id)
    r = c.execute(
        f"""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr)), 0) AS net
        FROM paper_trades
        WHERE {where} AND status = 'CLOSED' AND exit_ts > %s
        """,
        (*params, since),
    ).fetchone()
    return int(r["n"]), Decimal(r["net"])


def run_decay_pass() -> dict:
    """Evaluate every active instinct against the owning agent's post-promotion
    trades; decay/reopen the ones that aren't holding. Fail-safe per row."""
    evaluated, decayed, reopened = 0, [], []
    with conn() as c:
        instincts = list(c.execute(
            "SELECT id, competitor_id, cluster_id, confidence, misses, hits, "
            "promoted_ts, last_eval_ts FROM agent_instincts WHERE status = 'active'"))

    for ins in instincts:
        since = ins["last_eval_ts"] or ins["promoted_ts"]
        try:
            with conn() as c:
                n, net = _trades_since(c, ins["competitor_id"], since)
                if n < _MIN_TRADES_TO_JUDGE:
                    continue  # not enough new evidence to judge
                evaluated += 1
                if net < 0:
                    new_conf = round(float(ins["confidence"]) * INSTINCT_DECAY_FACTOR, 3)
                    if new_conf < INSTINCT_CONFIDENCE_FLOOR:
                        c.execute(
                            "UPDATE agent_instincts SET status='decayed', "
                            "confidence=%s, misses=misses+1, last_eval_ts=now(), "
                            "updated_ts=now() WHERE id=%s",
                            (new_conf, ins["id"]))
                        decayed.append(ins["id"])
                        # reopen the source cluster so the loop re-fixes it
                        if ins["cluster_id"] is not None:
                            from helm.agents.clustering import set_cluster_status
                            if set_cluster_status(int(ins["cluster_id"]), "open",
                                                  note="instinct decayed — reopened"):
                                reopened.append(int(ins["cluster_id"]))
                    else:
                        c.execute(
                            "UPDATE agent_instincts SET confidence=%s, "
                            "misses=misses+1, last_eval_ts=now(), updated_ts=now() "
                            "WHERE id=%s", (new_conf, ins["id"]))
                else:
                    c.execute(
                        "UPDATE agent_instincts SET hits=hits+1, last_eval_ts=now(), "
                        "updated_ts=now() WHERE id=%s", (ins["id"],))
        except Exception as exc:  # noqa: BLE001 — one bad instinct must not abort
            insert_audit("agents", "instinct_decay_error",
                         {"instinct_id": ins["id"], "error": str(exc)[:200]})
            continue

    if decayed or reopened:
        insert_audit("agents", "instinct_decay_pass",
                     {"evaluated": evaluated, "decayed": decayed,
                      "reopened_clusters": reopened})
    return {"evaluated": evaluated, "decayed": decayed, "reopened": reopened}


def active_instincts(competitor_id: str | None = None) -> list[dict]:
    """Ledger rows for the dashboard — what each agent has learned and is
    applying. All statuses (active/decayed) so the UI can show self-correction."""
    sql = ("SELECT id, competitor_id, cluster_id, statement, layer, artifact_kind, "
           "confidence, status, hits, misses, promoted_ts, last_eval_ts "
           "FROM agent_instincts")
    args: tuple = ()
    if competitor_id is not None:
        sql += " WHERE competitor_id = %s"
        args = (competitor_id,)
    sql += " ORDER BY status, confidence DESC, promoted_ts DESC"
    with conn() as c:
        return list(c.execute(sql, args))


__all__ = ["promote_cluster_to_instinct", "run_decay_pass", "active_instincts"]

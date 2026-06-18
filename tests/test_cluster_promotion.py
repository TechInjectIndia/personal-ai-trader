"""Loop-closer (FRD G2 runtime): cluster promotion + status reconciliation.

run_cluster_promotion is tested on a SENTINEL freestyle surface with a fake PM
LLM, so it's isolated from real clusters (and the live backfill) and makes no
model call. reconcile_cluster_statuses is tested by seeding cluster→proposal→
task→release chains directly.
"""
from __future__ import annotations

import pytest

import helm.agents.pm as pm
from helm.agents import clustering as cl
from helm.data.store import conn

SURFACE = "ZZZP-agent"          # sentinel freestyle surface
PT = "ZZZP"                     # promotion sentinel
RC = "ZZZRC"                    # reconcile sentinel


def _cleanup():
    with conn() as c:
        for marker in (PT, RC):
            c.execute("UPDATE improvement_proposals SET cluster_id = NULL WHERE title LIKE %s",
                      (f"{marker}%",))
            c.execute("DELETE FROM releases WHERE summary LIKE %s", (f"{marker}%",))
            c.execute("DELETE FROM agent_tasks WHERE title LIKE %s", (f"{marker}%",))
            c.execute("DELETE FROM improvement_proposals WHERE title LIKE %s", (f"{marker}%",))
            c.execute("DELETE FROM proposal_clusters WHERE theme LIKE %s", (f"{marker}%",))
        c.execute("DELETE FROM agent_tasks WHERE competitor_id = %s", (SURFACE,))
        c.execute("DELETE FROM agent_runs WHERE competitor_id = %s", (SURFACE,))
        c.execute("DELETE FROM competitor_wallets WHERE competitor_id = %s", (SURFACE,))
        c.execute("DELETE FROM competitors WHERE id = %s", (SURFACE,))


@pytest.fixture(autouse=True)
def _scrub():
    _cleanup()
    # build_goal_brief for a freestyle surface needs a competitor + wallet row.
    with conn() as c:
        c.execute("INSERT INTO competitors (id, name, status) VALUES (%s, %s, 'inactive') "
                  "ON CONFLICT (id) DO NOTHING", (SURFACE, "ZZZP test agent"))
        c.execute(
            "INSERT INTO competitor_wallets (competitor_id, initial_capital_inr, "
            "available_inr, realized_pnl_inr) VALUES (%s, 50000, 50000, 0) "
            "ON CONFLICT (competitor_id, market) DO NOTHING", (SURFACE,))
    yield
    _cleanup()


def _any_retro_id() -> int:
    with conn() as c:
        row = c.execute("SELECT id FROM trade_retrospectives ORDER BY id LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no retro to anchor FK")
    return int(row["id"])


def _seed_proposal(title: str, cluster_id: int | None, competitor_id: str | None,
                   status: str = "open") -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO improvement_proposals "
            "(retro_id, category, title, rationale, proposed_change, confidence, "
            " competitor_id, cluster_id, status) "
            "VALUES (%s, 'sizing', %s, 'r', 'c', 4, %s, %s, %s) RETURNING id",
            (_any_retro_id(), title, competitor_id, cluster_id, status),
        ).fetchone()["id"]


def _seed_cluster(theme: str, surface: str, *, recurrence: int = 5, status: str = "open",
                  rep: int | None = None) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO proposal_clusters "
            "(theme, layer, target_surface, confidence, recurrence, status, "
            " representative_proposal_id) "
            "VALUES (%s, 'sizing', %s, 0.8, %s, %s, %s) RETURNING id",
            (f"{theme}", surface, recurrence, status, rep),
        ).fetchone()["id"]


# ── promotion ─────────────────────────────────────────────────────────

def _fake_pm_llm(*a, **k):
    return {"task_type": "persona_edit", "title": f"{PT} ship the fix",
            "rationale": "recurrence x5 — durable persona change",
            "priority": 4, "spec": {"new_persona": "x" * 60, "diff_summary": "y"}}


def test_promotion_creates_task_and_marks_cluster_in_flight(monkeypatch):
    rep = _seed_proposal(f"{PT} rep proposal", None, SURFACE)
    cid = _seed_cluster(f"{PT} theme", SURFACE, rep=rep)
    # link the representative proposal to its cluster
    with conn() as c:
        c.execute("UPDATE improvement_proposals SET cluster_id = %s WHERE id = %s",
                  (cid, rep))
    monkeypatch.setattr(pm, "complete_json", _fake_pm_llm)

    res = pm.run_cluster_promotion(SURFACE)
    assert res["deferred"] is False
    assert res["cluster_id"] == cid
    assert res["task_created"] is not None
    with conn() as c:
        clu = c.execute("SELECT status FROM proposal_clusters WHERE id=%s",
                        (cid,)).fetchone()
        task = c.execute("SELECT task_type, proposal_id, competitor_id FROM agent_tasks "
                         "WHERE id=%s", (res["task_created"],)).fetchone()
    assert clu["status"] == "in_flight"
    assert task["task_type"] == "persona_edit"
    assert task["proposal_id"] == rep           # task → proposal → cluster linkage
    assert task["competitor_id"] == SURFACE


def test_promotion_noop_when_no_open_clusters(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not call the LLM when there's no cluster")
    monkeypatch.setattr(pm, "complete_json", _boom)
    res = pm.run_cluster_promotion(SURFACE)      # sentinel surface has no clusters
    assert res["deferred"] is True
    assert res["task_created"] is None


def test_promotion_rejects_invalid_task_type(monkeypatch):
    # A house code task_type is invalid on a freestyle surface → noop, no task.
    rep = _seed_proposal(f"{PT} rep", None, SURFACE)
    cid = _seed_cluster(f"{PT} theme", SURFACE, rep=rep)
    with conn() as c:
        c.execute("UPDATE improvement_proposals SET cluster_id=%s WHERE id=%s", (cid, rep))
    monkeypatch.setattr(pm, "complete_json", lambda *a, **k: {
        "task_type": "param_change", "title": f"{PT} bad", "rationale": "r",
        "priority": 3, "spec": {"x": 1}})
    res = pm.run_cluster_promotion(SURFACE)
    assert res["deferred"] is True
    assert "invalid" in res["reason"]
    with conn() as c:
        assert c.execute("SELECT status FROM proposal_clusters WHERE id=%s",
                         (cid,)).fetchone()["status"] == "open"   # untouched


# ── reconciliation ────────────────────────────────────────────────────

def _seed_task(title: str, proposal_id: int, status: str) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO agent_tasks (created_by, title, rationale, task_type, "
            " spec, priority, proposal_id, status) "
            "VALUES ('pm', %s, 'r', 'param_change', '{}'::jsonb, 3, %s, %s) RETURNING id",
            (title, proposal_id, status),
        ).fetchone()["id"]


def _seed_release(task_id: int, status: str) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO releases (task_id, commit_sha, branch, summary, status) "
            "VALUES (%s, 'abc123', 'main', %s, %s) RETURNING id",
            (task_id, f"{RC} release", status),
        ).fetchone()["id"]


def test_reconcile_marks_verified_and_applies_members():
    cid = _seed_cluster(f"{RC} theme", "house", status="in_flight")
    p = _seed_proposal(f"{RC} member", cid, "house-claude")
    t = _seed_task(f"{RC} task", p, "done")
    rel = _seed_release(t, "verified")
    with conn() as c:
        c.execute("UPDATE agent_tasks SET release_id=%s WHERE id=%s", (rel, t))

    out = cl.reconcile_cluster_statuses()
    assert cid in out["verified"]
    with conn() as c:
        assert c.execute("SELECT status FROM proposal_clusters WHERE id=%s",
                         (cid,)).fetchone()["status"] == "verified"
        assert c.execute("SELECT status FROM improvement_proposals WHERE id=%s",
                         (p,)).fetchone()["status"] == "applied"


def test_reconcile_reopens_on_failure():
    cid = _seed_cluster(f"{RC} theme2", "house", status="in_flight")
    p = _seed_proposal(f"{RC} member2", cid, "house-claude")
    _seed_task(f"{RC} task2", p, "failed")
    out = cl.reconcile_cluster_statuses()
    assert cid in out["reopened"]
    with conn() as c:
        assert c.execute("SELECT status FROM proposal_clusters WHERE id=%s",
                         (cid,)).fetchone()["status"] == "open"


def test_reconcile_leaves_pending_in_flight():
    cid = _seed_cluster(f"{RC} theme3", "house", status="in_flight")
    p = _seed_proposal(f"{RC} member3", cid, "house-claude")
    t = _seed_task(f"{RC} task3", p, "done")
    rel = _seed_release(t, "deployed")        # built but Tester hasn't verified yet
    with conn() as c:
        c.execute("UPDATE agent_tasks SET release_id=%s WHERE id=%s", (rel, t))
    out = cl.reconcile_cluster_statuses()
    assert cid not in out["verified"] and cid not in out["reopened"]
    with conn() as c:
        assert c.execute("SELECT status FROM proposal_clusters WHERE id=%s",
                         (cid,)).fetchone()["status"] == "in_flight"

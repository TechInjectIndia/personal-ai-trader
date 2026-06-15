"""G2 runtime wiring — cluster-on-emit + selector/status/escalation surfacing.

cluster-on-emit is tested at the helper boundary (`_maybe_cluster_proposals`)
with the flag forced on/off and a fake assigner, so no LLM and no retro is run.
The selector/status/escalation functions are tested against the live DB under a
ZZZRT sentinel and scrubbed.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import helm.retro as retro
from helm.agents import clustering as cl
from helm.data.store import conn

SENTINEL = "ZZZRT"


def _cleanup():
    with conn() as c:
        c.execute("UPDATE improvement_proposals SET cluster_id = NULL WHERE title LIKE %s",
                  (f"{SENTINEL}%",))
        c.execute("DELETE FROM proposal_clusters WHERE theme LIKE %s", (f"{SENTINEL}%",))
        c.execute("DELETE FROM improvement_proposals WHERE title LIKE %s", (f"{SENTINEL}%",))
        c.execute("DELETE FROM settings WHERE key = 'CLUSTER_ON_EMIT'")
        c.execute("DELETE FROM settings WHERE key = 'attention_queue'")


@pytest.fixture(autouse=True)
def _scrub():
    _cleanup()
    yield
    _cleanup()


# ── B1: cluster-on-emit is flag-gated and fail-safe ───────────────────

def test_cluster_on_emit_noop_when_flag_off(monkeypatch):
    called = []
    monkeypatch.setattr("helm.config.live_flag", lambda name: False)
    monkeypatch.setattr("helm.agents.clustering.assign_and_persist",
                        lambda pid, **k: called.append(pid))
    retro._maybe_cluster_proposals([1, 2, 3])
    assert called == []   # flag off → no clustering


def test_cluster_on_emit_runs_when_flag_on(monkeypatch):
    called = []
    monkeypatch.setattr("helm.config.live_flag", lambda name: name == "CLUSTER_ON_EMIT")
    monkeypatch.setattr("helm.agents.clustering.assign_and_persist",
                        lambda pid, **k: called.append(pid))
    retro._maybe_cluster_proposals([10, 11])
    assert called == [10, 11]


def test_cluster_on_emit_swallows_errors(monkeypatch):
    # A clustering failure must NEVER propagate out of the retro path.
    monkeypatch.setattr("helm.config.live_flag", lambda name: True)

    def _boom(pid, **k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("helm.agents.clustering.assign_and_persist", _boom)
    retro._maybe_cluster_proposals([1])   # must not raise


def test_cluster_on_emit_empty_list_is_noop(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not even check the flag for an empty list")
    monkeypatch.setattr("helm.config.live_flag", _boom)
    retro._maybe_cluster_proposals([])    # returns before touching the flag


# ── B2: selector + status + escalation ────────────────────────────────

def _seed_cluster(*, theme: str, surface: str, recurrence: int, conf: str,
                  escalated: bool = False, origin: str | None = None,
                  status: str = "open") -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO proposal_clusters "
            "(theme, layer, target_surface, escalated, origin_competitor_id, "
            " confidence, recurrence, status) "
            "VALUES (%s, 'risk', %s, %s, %s, %s, %s, %s) RETURNING id",
            (f"{SENTINEL} {theme}", surface, escalated, origin, Decimal(conf),
             recurrence, status),
        ).fetchone()["id"]


# Sentinel surfaces keep these ranking tests isolated from real clusters (and
# from the concurrent backfill writing the real 'house'/competitor surfaces).
SURF = f"{SENTINEL}-surface"


def test_next_cluster_prefers_escalated_then_rank():
    _seed_cluster(theme="small", surface=SURF, recurrence=2, conf="0.5")
    big = _seed_cluster(theme="big", surface=SURF, recurrence=9, conf="0.9")
    esc = _seed_cluster(theme="escalated", surface=SURF, recurrence=1, conf="0.3",
                        escalated=True, origin="gemini-momentum")
    nxt = cl.next_cluster_for_surface(SURF)
    # escalated wins even at lower rank score
    assert nxt["id"] == esc
    # with the escalated one resolved, the highest rank score is next
    cl.set_cluster_status(esc, "accepted")
    assert cl.next_cluster_for_surface(SURF)["id"] == big


def test_next_cluster_scoped_by_surface():
    _seed_cluster(theme="surf A only", surface=f"{SENTINEL}-A", recurrence=5, conf="0.8")
    assert cl.next_cluster_for_surface(f"{SENTINEL}-B") is None


def test_set_cluster_status_validates_and_updates():
    cid = _seed_cluster(theme="x", surface="house", recurrence=3, conf="0.6")
    assert cl.set_cluster_status(cid, "in_flight") is True
    with conn() as c:
        assert c.execute("SELECT status FROM proposal_clusters WHERE id=%s",
                         (cid,)).fetchone()["status"] == "in_flight"
    assert cl.set_cluster_status(999_999_999, "verified") is False  # missing row
    with pytest.raises(ValueError):
        cl.set_cluster_status(cid, "bogus")


def test_escalation_alerts_threshold():
    _seed_cluster(theme="below", surface="house", recurrence=3, conf="0.5",
                  escalated=True, origin="gemini-momentum")
    hot = _seed_cluster(theme="hot", surface="house", recurrence=7, conf="0.8",
                        escalated=True, origin="nemotron-trend")
    ids = {a["id"] for a in cl.escalation_alerts(min_recurrence=5)}
    assert hot in ids
    assert all(a["recurrence"] >= 5 for a in cl.escalation_alerts(min_recurrence=5))


def test_push_escalation_alerts_enqueues_action_items():
    cid = _seed_cluster(theme="route me", surface="house", recurrence=6, conf="0.7",
                        escalated=True, origin="gemini-momentum")
    n = cl.push_escalation_alerts()
    assert n >= 1
    from helm.dashboard.attention import attention_items
    keys = {i.key for i in attention_items()}
    assert f"cluster_escalated_{cid}" in keys

"""Proposal clustering (FRD G1/G2).

Two layers: PURE math (no DB) and the DB-backed assign/persist flow exercised
with a fake LLM so no model is called. The DB tests seed throwaway retros +
proposals under a ZZZCLUST sentinel and scrub them.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from helm.agents import clustering as cl
from helm.data.store import conn


# ── PURE: confidence normalisation + decay ────────────────────────────

def test_normalise_confidence_clamps_and_defaults():
    assert cl.normalise_confidence(5) == Decimal("1.00")
    assert cl.normalise_confidence(1) == Decimal("0.20")
    assert cl.normalise_confidence(None) == Decimal("0.60")
    assert cl.normalise_confidence(99) == Decimal("1.00")   # clamp high
    assert cl.normalise_confidence(0) == Decimal("0.20")    # clamp low


def test_decay_is_identity_at_zero_and_monotonic():
    base = Decimal("1.00")
    assert cl.decay_confidence(base, 0) == Decimal("1.00")
    one_wk = cl.decay_confidence(base, 1)
    four_wk = cl.decay_confidence(base, 4)
    assert one_wk == Decimal("0.95")
    assert four_wk < one_wk < base
    # negative weeks treated as 0 (never amplifies)
    assert cl.decay_confidence(base, -3) == Decimal("1.00")


# ── PURE: rank score ──────────────────────────────────────────────────

def test_rank_score_recurrence_dominates():
    # 10× recurrence at modest confidence beats a 1× high-confidence one-off
    many = cl.cluster_rank_score(10, Decimal("0.4"), 1)
    one = cl.cluster_rank_score(1, Decimal("1.0"), 1)
    assert many > one
    # economic priority scales linearly
    assert cl.cluster_rank_score(2, Decimal("0.5"), 3) == pytest.approx(3.0)


# ── PURE: category → layer ────────────────────────────────────────────

def test_category_to_layer_maps_and_defaults():
    assert cl.category_to_layer("decider_prompt") == "decider"
    assert cl.category_to_layer("sizing") == "sizing"
    assert cl.category_to_layer("nonsense") == "meta"
    assert cl.category_to_layer(None) == "meta"


# ── PURE: G2 surface classification ───────────────────────────────────

def test_house_proposal_stays_house():
    assert cl.classify_surface(None, "anything") == ("house", False, None)
    assert cl.classify_surface("house-claude", "x") == ("house", False, None)


def test_freestyle_in_surface_stays_with_competitor():
    surface, esc, origin = cl.classify_surface(
        "gemini-momentum", "Tighten stops on choppy mornings", "widen the persona")
    assert surface == "gemini-momentum"
    assert esc is False and origin is None


def test_freestyle_naming_shared_code_escalates_to_house():
    # The canonical failure: a freestyle agent diagnosing the shared sizer.
    surface, esc, origin = cl.classify_surface(
        "gemini-momentum",
        "Scale position down to cap instead of hard-blocking",
        "the risk gate / position sizing in helm/orchestrator rejects the order")
    assert surface == "house"
    assert esc is True
    assert origin == "gemini-momentum"


# ── DB flow: assign + persist with a fake LLM ─────────────────────────

SENTINEL = "ZZZCLUST"


def _cleanup():
    with conn() as c:
        # NULL out cluster refs first (FK), then delete clusters + proposals.
        c.execute("UPDATE improvement_proposals SET cluster_id = NULL "
                  "WHERE title LIKE %s", (f"{SENTINEL}%",))
        c.execute("DELETE FROM proposal_clusters WHERE theme LIKE %s", (f"{SENTINEL}%",))
        c.execute("DELETE FROM improvement_proposals WHERE title LIKE %s", (f"{SENTINEL}%",))


@pytest.fixture(autouse=True)
def _scrub():
    _cleanup()
    yield
    _cleanup()


def _any_retro_id() -> int:
    """Reuse an existing retro for the NOT-NULL FK — we never read it back, and
    seeding a full retro (decision_id FK + 6 NOT-NULL text cols) is needless."""
    with conn() as c:
        row = c.execute("SELECT id FROM trade_retrospectives ORDER BY id LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no trade_retrospectives row to anchor the FK")
    return int(row["id"])


def _seed_proposal(*, title: str, category: str = "sizing", confidence: int = 4,
                   competitor_id: str | None = None,
                   proposed_change: str = "x", rationale: str = "y") -> int:
    with conn() as c:
        retro_id = _any_retro_id()
        return c.execute(
            "INSERT INTO improvement_proposals "
            "(retro_id, category, title, rationale, proposed_change, confidence, competitor_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (retro_id, category, f"{SENTINEL} {title}", rationale, proposed_change,
             confidence, competitor_id),
        ).fetchone()["id"]


def _fake_llm_new(*a, **k):
    return {"match_cluster_id": None, "new_theme": f"{SENTINEL} canonical idea"}


def _fake_llm_match(cluster_id):
    def _f(*a, **k):
        return {"match_cluster_id": cluster_id}
    return _f


def test_first_proposal_opens_a_cluster():
    pid = _seed_proposal(title="cap block A")
    res = cl.assign_and_persist(pid, llm=_fake_llm_new)
    assert res["created_new"] is True
    with conn() as c:
        row = c.execute("SELECT cluster_id FROM improvement_proposals WHERE id=%s",
                        (pid,)).fetchone()
        assert row["cluster_id"] == res["cluster_id"]
        clu = c.execute("SELECT recurrence, confidence FROM proposal_clusters WHERE id=%s",
                        (res["cluster_id"],)).fetchone()
    assert clu["recurrence"] == 1
    assert clu["confidence"] == Decimal("0.80")   # conf 4/5, fresh → no decay


def test_restatement_increments_recurrence_not_orphans():
    p1 = _seed_proposal(title="cap block A")
    r1 = cl.assign_and_persist(p1, llm=_fake_llm_new)
    cid = r1["cluster_id"]
    # second proposal: fake LLM matches it onto the first cluster
    p2 = _seed_proposal(title="cap block B restated")
    r2 = cl.assign_and_persist(p2, llm=_fake_llm_match(cid))
    assert r2["created_new"] is False
    assert r2["cluster_id"] == cid
    with conn() as c:
        clu = c.execute("SELECT recurrence FROM proposal_clusters WHERE id=%s",
                        (cid,)).fetchone()
    assert clu["recurrence"] == 2   # incremented, no orphan cluster opened


def test_idempotent_already_clustered():
    pid = _seed_proposal(title="cap block A")
    r1 = cl.assign_and_persist(pid, llm=_fake_llm_new)
    # re-running must not double-count or move it
    def _boom(*a, **k):
        raise AssertionError("LLM must not be called for an already-clustered proposal")
    r2 = cl.assign_and_persist(pid, llm=_boom)
    assert r2["cluster_id"] == r1["cluster_id"]
    assert r2.get("skipped") == "already_clustered"


def test_escalated_freestyle_proposal_routes_to_house_surface():
    pid = _seed_proposal(
        title="scale to cap not block",
        competitor_id="gemini-momentum",
        proposed_change="the shared sizer / risk gate in helm/orchestrator hard-blocks")
    res = cl.assign_and_persist(pid, llm=_fake_llm_new)
    assert res["target_surface"] == "house"
    assert res["escalated"] is True
    with conn() as c:
        clu = c.execute(
            "SELECT target_surface, escalated, origin_competitor_id "
            "FROM proposal_clusters WHERE id=%s", (res["cluster_id"],)).fetchone()
    assert clu["target_surface"] == "house"
    assert clu["escalated"] is True
    assert clu["origin_competitor_id"] == "gemini-momentum"


def test_surface_scoping_keeps_freestyle_and_house_clusters_separate():
    # A house cluster exists; a freestyle in-surface proposal must NOT be allowed
    # to match it — candidates are scoped to the proposal's own surface. Even if
    # the LLM tries to return the house cluster id, it's rejected (not a valid
    # same-surface candidate) and a new gemini-surface cluster is opened.
    hp = _seed_proposal(title="house idea")
    house_res = cl.assign_and_persist(hp, llm=_fake_llm_new)
    house_cid = house_res["cluster_id"]
    # open_clusters scoped to gemini must never include the house cluster
    assert house_cid not in {c["id"] for c in cl.open_clusters("gemini-momentum")}
    assert house_cid in {c["id"] for c in cl.open_clusters("house")}

    fp = _seed_proposal(title="gemini persona idea", competitor_id="gemini-momentum",
                        category="strategy", proposed_change="tighten persona")
    res = cl.assign_and_persist(fp, llm=_fake_llm_match(house_cid))  # LLM tries house id
    assert res["created_new"] is True              # house id rejected → new cluster
    assert res["cluster_id"] != house_cid
    assert res["target_surface"] == "gemini-momentum"

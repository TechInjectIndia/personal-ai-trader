"""
Unit tests for helm.agents.backlog — the pure clustering + ranking used by the
PM backlog-drain path. These exercise the side-effect-free functions only
(`cluster_proposals`, `_jaccard`, `_normalize`); the DB loaders are not touched,
so the suite runs without Postgres.
"""

from __future__ import annotations

from helm.agents.backlog import (
    DEFAULT_SIMILARITY_THRESHOLD,
    ProposalCluster,
    _jaccard,
    _normalize,
    cluster_proposals,
)


def _p(pid, *, category, title, change="", confidence=3, created_ts="2026-01-01"):
    """Build a synthetic proposal dict in the shape the clusterer expects."""
    return {
        "id": pid,
        "category": category,
        "title": title,
        "proposed_change": change,
        "confidence": confidence,
        "created_ts": created_ts,
    }


# ─── primitives ──────────────────────────────────────────────────────


def test_normalize_drops_stopwords_and_punctuation():
    toks = _normalize("Widen the ORB stop on choppy mornings!")
    assert "widen" in toks
    assert "orb" in toks
    assert "choppy" in toks
    assert "mornings" in toks
    # stop-words removed
    assert "the" not in toks
    assert "on" not in toks


def test_jaccard_identical_and_disjoint():
    a = _normalize("widen orb stop choppy mornings")
    b = _normalize("widen orb stop choppy mornings")
    assert _jaccard(a, b) == 1.0
    c = _normalize("reduce position size on gap days")
    assert _jaccard(a, c) == 0.0
    assert _jaccard(frozenset(), frozenset()) == 0.0


# ─── clustering ──────────────────────────────────────────────────────


def test_near_duplicates_cluster_together():
    # Restatements of the same idea reuse most of the same vocabulary — the
    # realistic shape of retro-emitted near-dupes.
    proposals = [
        _p(1, category="risk",
           title="Widen ORB stop on choppy mornings",
           change="Increase the ORB stop distance during choppy opening sessions"),
        _p(2, category="risk",
           title="Widen the ORB stop during choppy morning sessions",
           change="Increase ORB stop distance on choppy mornings"),
        _p(3, category="risk",
           title="Widen ORB stop distance on choppy opening mornings",
           change="Increase the ORB stop during choppy opening sessions"),
    ]
    clusters = cluster_proposals(proposals)
    # All three near-dupes collapse into a single cluster.
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl.recurrence == 3
    assert sorted(cl.member_ids) == [1, 2, 3]


def test_single_link_chaining_merges_transitive_near_dupes():
    # A~B (>=threshold) and B~C (>=threshold) but A~C just below: single-link
    # should still pull all three into one cluster via the B bridge.
    proposals = [
        _p(1, category="risk", title="reduce position size on gap up days",
           change="reduce position size on gap up days strongly"),
        _p(2, category="risk", title="reduce position size on gap days",
           change="reduce position size on gap days clearly"),
        _p(3, category="risk", title="reduce position size on gap down days",
           change="reduce position size on gap down days strongly"),
    ]
    clusters = cluster_proposals(proposals)
    assert len(clusters) == 1
    assert sorted(clusters[0].member_ids) == [1, 2, 3]


def test_distinct_ideas_stay_separate():
    proposals = [
        _p(1, category="risk", title="Widen ORB stop on choppy mornings",
           change="increase orb stop distance choppy"),
        _p(2, category="risk", title="Reduce max open positions to two",
           change="lower concurrency cap to 2 positions"),
    ]
    clusters = cluster_proposals(proposals)
    assert len(clusters) == 2


def test_same_wording_different_category_does_not_merge():
    proposals = [
        _p(1, category="risk", title="Tighten stop on choppy mornings",
           change="tighten stop choppy morning"),
        _p(2, category="sizing", title="Tighten stop on choppy mornings",
           change="tighten stop choppy morning"),
    ]
    clusters = cluster_proposals(proposals)
    # Identical text but different categories -> two clusters.
    assert len(clusters) == 2
    cats = {c.category for c in clusters}
    assert cats == {"risk", "sizing"}


# ─── ranking ─────────────────────────────────────────────────────────


def test_ranking_is_recurrence_times_confidence_desc():
    proposals = [
        # Cluster A: recurrence 3, confidence 2 -> score 6
        _p(1, category="risk", title="alpha gamma",
           change="alpha gamma delta", confidence=2),
        _p(2, category="risk", title="alpha gamma delta",
           change="alpha gamma", confidence=2),
        _p(3, category="risk", title="alpha gamma delta epsilon",
           change="alpha gamma delta", confidence=2),
        # Cluster B: recurrence 1, confidence 5 -> score 5
        _p(10, category="strategy", title="lone wolf change",
           change="entirely unrelated text here", confidence=5),
        # Cluster C: recurrence 2, confidence 4 -> score 8 (highest)
        _p(20, category="sizing", title="bravo charlie sizing",
           change="bravo charlie sizing tweak", confidence=4),
        _p(21, category="sizing", title="bravo charlie sizing tweak",
           change="bravo charlie sizing", confidence=4),
    ]
    clusters = cluster_proposals(proposals)
    scores = [c.score for c in clusters]
    # Sorted descending by score.
    assert scores == sorted(scores, reverse=True)
    # Concretely: C (8) > A (6) > B (5).
    assert clusters[0].score == 8
    assert clusters[0].representative_id in (20, 21)
    assert clusters[-1].score == 5
    assert clusters[-1].representative_id == 10


def test_representative_is_highest_confidence_then_most_recent():
    proposals = [
        _p(1, category="meta", title="zeta eta theta",
           change="zeta eta theta iota", confidence=2, created_ts="2026-01-01"),
        _p(2, category="meta", title="zeta eta theta iota",
           change="zeta eta theta", confidence=5, created_ts="2026-01-02"),
        _p(3, category="meta", title="zeta eta theta kappa",
           change="zeta eta theta iota", confidence=5, created_ts="2026-01-05"),
    ]
    clusters = cluster_proposals(proposals)
    assert len(clusters) == 1
    cl = clusters[0]
    # Highest confidence is 5 (ids 2 and 3); tie broken by most recent -> id 3.
    assert cl.representative_id == 3
    assert cl.confidence == 5


def test_score_uses_representative_confidence_not_max_member():
    # Representative is the highest-confidence member, so score reflects it.
    proposals = [
        _p(1, category="execution", title="mu nu xi",
           change="mu nu xi omicron", confidence=1),
        _p(2, category="execution", title="mu nu xi omicron",
           change="mu nu xi", confidence=4),
    ]
    clusters = cluster_proposals(proposals)
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl.recurrence == 2
    assert cl.confidence == 4
    assert cl.score == 8


def test_empty_input_yields_no_clusters():
    assert cluster_proposals([]) == []


def test_to_prompt_dict_exposes_headline_signal():
    proposals = [
        _p(1, category="data", title="add news sentiment",
           change="feed news sentiment into the decider", confidence=4),
        _p(2, category="data", title="add news sentiment feed",
           change="feed news sentiment to decider", confidence=3),
    ]
    clusters = cluster_proposals(proposals)
    assert len(clusters) == 1
    d = clusters[0].to_prompt_dict()
    assert d["recurrence"] == 2
    assert d["confidence"] == 4
    assert d["score"] == 8
    assert set(d["member_ids"]) == {1, 2}
    assert d["representative_id"] == 1
    assert d["category"] == "data"


def test_threshold_is_a_sane_default():
    # Guard against an accidental edit pushing the threshold out of (0, 1).
    assert 0.0 < DEFAULT_SIMILARITY_THRESHOLD < 1.0


def test_proposalcluster_dataclass_properties():
    rep = _p(7, category="risk", title="x y z", change="x y z", confidence=3)
    cl = ProposalCluster(category="risk", representative=rep, members=[rep],
                         member_ids=[7])
    assert cl.recurrence == 1
    assert cl.confidence == 3
    assert cl.score == 3
    assert cl.representative_id == 7

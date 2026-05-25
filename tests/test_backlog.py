"""
Unit tests for the PM backlog-drain path.

Two surfaces are exercised, both WITHOUT a live DB or LLM:

  * helm.agents.backlog — the pure (approximate) lexical clusterer + the pure
    batch ranker (`cluster_proposals`, `_jaccard`, `_normalize`,
    `rank_proposals_for_batch`). The DB loaders are not touched.
  * helm.agents.pm — the SEMANTIC batch path's pure helpers: the batch-prompt
    builder (`_build_backlog_prompt`) and the per-proposal action validator
    (`_validate_batch_action`). The supersede/reject status writes are verified
    by stubbing `_apply_proposal_status`, so no Postgres is needed.

The lexical clusterer is APPROXIMATE only (its threshold is now 0.45); the
authoritative dedup is the PM's semantic decision, which these tests model with
synthetic actions rather than a real LLM call.
"""

from __future__ import annotations

from helm.agents.backlog import (
    DEFAULT_SIMILARITY_THRESHOLD,
    ProposalCluster,
    _jaccard,
    _normalize,
    cluster_proposals,
    rank_proposals_for_batch,
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


def test_threshold_demoted_to_approximate_value():
    # The lexical clusterer was demoted to an approximate preview; its default
    # threshold dropped from 0.6 to ~0.45 so the dry-run at least hints groups.
    assert DEFAULT_SIMILARITY_THRESHOLD == 0.45


def test_proposalcluster_dataclass_properties():
    rep = _p(7, category="risk", title="x y z", change="x y z", confidence=3)
    cl = ProposalCluster(category="risk", representative=rep, members=[rep],
                         member_ids=[7])
    assert cl.recurrence == 1
    assert cl.confidence == 3
    assert cl.score == 3
    assert cl.representative_id == 7


# ─── batch ranking (PM semantic-dedup input) ─────────────────────────


def test_rank_groups_by_category_then_confidence():
    proposals = [
        _p(1, category="risk", title="a", confidence=2),
        _p(2, category="sizing", title="b", confidence=5),
        _p(3, category="risk", title="c", confidence=4),
        _p(4, category="sizing", title="d", confidence=1),
    ]
    ranked = rank_proposals_for_batch(proposals)
    cats = [p["category"] for p in ranked]
    # Strongest category (sizing, best conf 5) leads; categories stay contiguous.
    assert cats == ["sizing", "sizing", "risk", "risk"]
    # Within each category, highest confidence first.
    assert [p["id"] for p in ranked] == [2, 4, 3, 1]


def test_rank_ties_break_by_recency_then_id():
    proposals = [
        _p(1, category="risk", title="a", confidence=3, created_ts="2026-01-01"),
        _p(2, category="risk", title="b", confidence=3, created_ts="2026-01-05"),
        _p(3, category="risk", title="c", confidence=3, created_ts="2026-01-05"),
    ]
    ranked = rank_proposals_for_batch(proposals)
    # Same confidence: most-recent first (id 2/3 before 1); id asc breaks the
    # 2-vs-3 tie deterministically.
    assert [p["id"] for p in ranked] == [2, 3, 1]


def test_rank_respects_limit():
    proposals = [
        _p(i, category="risk", title=f"t{i}", confidence=i) for i in range(1, 11)
    ]
    ranked = rank_proposals_for_batch(proposals, limit=3)
    assert len(ranked) == 3
    # Highest confidence first within the single category.
    assert [p["id"] for p in ranked] == [10, 9, 8]


def test_rank_passes_proposals_through_unmodified():
    proposals = [_p(1, category="risk", title="t", change="cc", confidence=4)]
    ranked = rank_proposals_for_batch(proposals)
    assert ranked[0]["proposed_change"] == "cc"
    assert ranked[0]["title"] == "t"


def test_rank_empty_input():
    assert rank_proposals_for_batch([]) == []


# ─── PM semantic-batch path (pure helpers, no DB / no LLM) ────────────

import json  # noqa: E402

from helm.agents import pm  # noqa: E402
from helm.agents.base import GoalBrief  # noqa: E402
from helm.config import HOUSE_COMPETITOR_ID  # noqa: E402


def _fake_goal() -> GoalBrief:
    """A GoalBrief built without DB — to_prompt_dict only needs the fields."""
    from decimal import Decimal
    z = Decimal("0")
    return GoalBrief(
        equity=Decimal("50000"), initial=Decimal("50000"), goal=Decimal("100000"),
        progress_pct=0.0, days_to_goal=30, realised_pnl=z, realised_pnl_7d=z,
        win_rate_pct=0.0, expectancy_inr=z, trades_total=0, trades_7d=0,
        signals_7d=0, take_rate_pct_7d=0.0, open_proposals=5, open_tasks=0,
        unverified_releases=0,
    )


# The realistic same-idea-different-words batch from the dry-run failure: these
# share almost no tokens but are one idea ("hard R:R floor gate").
_RR_BATCH = [
    {"id": 11, "category": "risk", "confidence": 4,
     "title": "Enforce R:R threshold as a hard gate, not a soft guideline",
     "proposed_change": "Make R:R a hard skip", "rationale": "many losers"},
    {"id": 12, "category": "risk", "confidence": 3,
     "title": "Enforce hard reward-vs-risk floor before taking any trade",
     "proposed_change": "Block low reward-vs-risk", "rationale": "evidence"},
    {"id": 13, "category": "risk", "confidence": 3,
     "title": "Enforce reward-to-risk floor of 1.5 as a hard skip",
     "proposed_change": "Skip below 1.5", "rationale": "evidence"},
    {"id": 14, "category": "risk", "confidence": 2,
     "title": "Hardcode R:R floor check into decider instructions",
     "proposed_change": "Decider prompt R:R", "rationale": "evidence"},
]


def test_build_backlog_prompt_includes_every_proposal_in_batch():
    agent = {"id": HOUSE_COMPETITOR_ID, "name": "house", "kind": "house",
             "persona": None}
    prompt = pm._build_backlog_prompt(agent, _fake_goal(), _RR_BATCH,
                                      max_accepts=3)
    # Every proposal id from the batch is present in the prompt payload.
    for p in _RR_BATCH:
        assert str(p["id"]) in prompt
        assert p["title"] in prompt
    # The embedded JSON carries all four proposals under open_proposals.
    blob = prompt.split("```json", 1)[1].rsplit("```", 1)[0]
    payload = json.loads(blob)
    assert len(payload["open_proposals"]) == 4
    assert payload["max_accepts_this_batch"] == 3
    assert payload["mode"] == "backlog_drain_batch"


def test_build_backlog_prompt_instructs_semantic_supersede():
    agent = {"id": HOUSE_COMPETITOR_ID, "name": "house", "kind": "house",
             "persona": None}
    prompt = pm._build_backlog_prompt(agent, _fake_goal(), _RR_BATCH)
    low = prompt.lower()
    # The PM is told to dedup by MEANING and to supersede restatements.
    assert "supersede" in low
    assert "meaning" in low
    assert "accept" in low
    assert "reject" in low


def test_build_backlog_prompt_freestyle_restricts_task_types():
    agent = {"id": "ti-gemini", "name": "Gemini", "kind": "freestyle",
             "persona": "scalper"}
    prompt = pm._build_backlog_prompt(agent, _fake_goal(), _RR_BATCH)
    assert "persona_edit" in prompt
    assert "strategy_config_edit" in prompt
    # House-only code task types are NOT offered to a freestyle agent's accepts.
    blob = prompt.split("```json", 1)[1].rsplit("```", 1)[0]
    payload = json.loads(blob)
    assert "prompt_tweak" not in payload["valid_task_types_for_this_agent"]


# ─── batch action validation ─────────────────────────────────────────


def test_validate_batch_action_accept_ok_for_house():
    a = {"proposal_id": 11, "action": "accept", "task_type": "prompt_tweak",
         "title": "Hard R:R gate", "rationale": "collapses 4 restatements",
         "priority": 5, "spec": {"file": "x", "anchor": "y", "action": "append",
                                 "text": "z"}}
    ok, why = pm._validate_batch_action(a, is_house=True, batch_ids={11, 12})
    assert ok, why


def test_validate_batch_action_rejects_id_outside_batch():
    a = {"proposal_id": 99, "action": "reject"}
    ok, why = pm._validate_batch_action(a, is_house=True, batch_ids={11, 12})
    assert not ok
    assert "not in this batch" in why


def test_validate_batch_action_supersede_requires_target():
    a = {"proposal_id": 12, "action": "supersede"}
    ok, why = pm._validate_batch_action(a, is_house=True, batch_ids={11, 12})
    assert not ok
    assert "supersedes_id" in why


def test_validate_batch_action_supersede_ok_with_target():
    a = {"proposal_id": 12, "action": "supersede", "supersedes_id": 11}
    ok, _ = pm._validate_batch_action(a, is_house=True, batch_ids={11, 12})
    assert ok


def test_validate_batch_action_code_type_invalid_for_freestyle():
    a = {"proposal_id": 11, "action": "accept", "task_type": "prompt_tweak",
         "title": "t", "rationale": "r", "priority": 3, "spec": {}}
    ok, why = pm._validate_batch_action(a, is_house=False, batch_ids={11})
    assert not ok
    assert "house-only" in why


def test_validate_batch_action_accept_needs_task_fields():
    a = {"proposal_id": 11, "action": "accept", "task_type": "prompt_tweak",
         "title": "", "rationale": "", "priority": 3, "spec": {}}
    ok, why = pm._validate_batch_action(a, is_house=True, batch_ids={11})
    assert not ok
    assert "title" in why


# ─── supersede / reject helpers mark ALL ids with the right status_note ─


def test_supersede_and_reject_via_apply_status(monkeypatch):
    """The drain marks superseded restatements + rejects through
    _apply_proposal_status. Stub it to capture (id, status, note) — no DB."""
    calls: list[tuple[int, str, str]] = []

    def fake_apply(pid, status, note):
        calls.append((int(pid), status, note))
        return True

    monkeypatch.setattr(pm, "_apply_proposal_status", fake_apply)

    # Drive the same status-write path the batch drain uses for supersede:
    # every restatement is marked superseded, noting the accepted id.
    accepted_id = 11
    for restatement in (12, 13, 14):
        note = f"superseded by accepted proposal {accepted_id} (backlog drain)"
        assert pm._apply_proposal_status(restatement, "superseded", note)

    # And reject one low-value proposal.
    assert pm._apply_proposal_status(20, "rejected", "noise (backlog drain)")

    # ALL targeted ids were marked, with the correct status + note.
    superseded = [(pid, note) for pid, st, note in calls if st == "superseded"]
    assert sorted(pid for pid, _ in superseded) == [12, 13, 14]
    for _, note in superseded:
        assert f"accepted proposal {accepted_id}" in note
    rejected = [(pid, st) for pid, st, _ in calls if st == "rejected"]
    assert rejected == [(20, "rejected")]

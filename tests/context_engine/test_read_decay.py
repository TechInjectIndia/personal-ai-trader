"""Pure decay + staleness math (highest-value test, no DB)."""

from __future__ import annotations

import pytest

from context_engine.read import decay_score, evaluate


def test_no_decay_at_zero_age():
    assert decay_score(0.8, 0.0, 90.0) == pytest.approx(0.8)


def test_half_decay_at_one_half_life():
    assert decay_score(0.8, 90.0, 90.0) == pytest.approx(0.4)


def test_quarter_at_two_half_lives():
    assert decay_score(0.8, 180.0, 90.0) == pytest.approx(0.2)


def test_negative_age_treated_as_zero():
    # clock skew must not amplify the score
    assert decay_score(0.5, -30.0, 90.0) == pytest.approx(0.5)


def test_nonpositive_half_life_is_fully_decayed():
    assert decay_score(0.9, 10.0, 0.0) == 0.0


def test_evaluate_fresh_returns_decayed_not_stale():
    score, stale = evaluate(0.8, 90, 90.0, staleness_min=180, freshness_floor=0.05)
    assert stale is False
    assert score == pytest.approx(0.4, abs=1e-3)


def test_evaluate_stale_past_staleness_window():
    score, stale = evaluate(0.8, 90, 300.0, staleness_min=180, freshness_floor=0.05)
    assert stale is True
    assert score == 0.0


def test_evaluate_stale_below_freshness_floor():
    # decayed magnitude tiny -> stale even within the window
    score, stale = evaluate(0.1, 30, 120.0, staleness_min=180, freshness_floor=0.05)
    assert stale is True
    assert score == 0.0


def test_evaluate_rounds_to_three_dp():
    score, stale = evaluate(0.777777, 1000, 0.0, staleness_min=180, freshness_floor=0.05)
    assert stale is False
    assert score == 0.778


def test_evaluate_preserves_sign():
    score, stale = evaluate(-0.8, 90, 0.0, staleness_min=180, freshness_floor=0.05)
    assert stale is False
    assert score == pytest.approx(-0.8, abs=1e-3)

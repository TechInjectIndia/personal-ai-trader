"""Scorer clamping + LLM mocking (no live LLM)."""

from __future__ import annotations

from decimal import Decimal

from context_engine import score as score_mod
from context_engine.score import _clamp_half_life, _clamp_score, score_symbol
from context_engine.sources.base import ContextItem


def _items():
    return [ContextItem(symbol="INFY", source="t", url="u1", headline="Q4 beat")]


def test_clamp_score_within_range():
    assert _clamp_score(0.4123) == Decimal("0.412")


def test_clamp_score_above_one():
    assert _clamp_score(5.0) == Decimal("1.000")


def test_clamp_score_below_minus_one():
    assert _clamp_score(-9.0) == Decimal("-1.000")


def test_clamp_score_garbage_defaults_zero():
    assert _clamp_score("not-a-number") == Decimal("0.000")


def test_clamp_half_life_bounds():
    assert _clamp_half_life(1) == 5          # below MIN
    assert _clamp_half_life(99999) == 1440   # above MAX
    assert _clamp_half_life(90) == 90
    assert _clamp_half_life("bad") == 90     # default


def test_score_symbol_parses_and_clamps(monkeypatch):
    monkeypatch.setattr(
        score_mod, "complete_json",
        lambda *a, **k: {"score": 1.9, "half_life_min": 120, "rationale": "big beat"},
    )
    out = score_symbol("INFY", _items())
    assert out.score == Decimal("1.000")   # clamped from 1.9
    assert out.half_life_min == 120
    assert out.rationale == "big beat"


def test_score_symbol_handles_fence_stripped_dict(monkeypatch):
    # complete_json already returns a dict (helm.llm strips fences); we just
    # ensure missing keys fall back safely.
    monkeypatch.setattr(score_mod, "complete_json", lambda *a, **k: {})
    out = score_symbol("INFY", _items())
    assert out.score == Decimal("0.000")
    assert out.half_life_min == 90
    assert out.rationale == ""


def test_score_symbol_empty_items_no_llm(monkeypatch):
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("LLM must not be called for empty items")

    monkeypatch.setattr(score_mod, "complete_json", _boom)
    out = score_symbol("INFY", [])
    assert out.score == Decimal("0.000")
    assert called["n"] == 0

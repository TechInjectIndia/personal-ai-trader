"""F8 — autonomy-preserving mandate advisory.

Each competitor's own recent economics are injected into ITS mandate prompt; a
nudge is added only when edge-to-cost is thin. Advice only — never a hard rule.
"""
from __future__ import annotations

from decimal import Decimal

from helm.analytics.economics import Economics
from helm.competition import mandate


def _econ(n: int, e2c: str) -> Economics:
    d = Decimal
    return Economics(
        n=n, gross_pnl=d("10"), charges=d("100"), net_pnl=d("-90"),
        gross_expectancy=d("0.5"), net_expectancy=d("-3"), cost_drag_pct=d("1000"),
        win_pct=d("30"), gross_payoff=d("2"), net_payoff=d("1.1"),
        avg_abs_gross_move=d("30"), realised_e2c=d(e2c),
    )


def test_advisory_fires_on_thin_e2c(monkeypatch):
    monkeypatch.setattr(mandate, "book_economics", lambda **k: _econ(20, "1.9"))
    econ, advisory = mandate._economics_advisory("gemini-momentum")
    assert econ["n"] == 20
    assert "edge-to-cost" in advisory and "1.9" in advisory


def test_no_advisory_when_e2c_healthy(monkeypatch):
    monkeypatch.setattr(mandate, "book_economics", lambda **k: _econ(20, "4.5"))
    _, advisory = mandate._economics_advisory("gemini-momentum")
    assert advisory == ""


def test_no_advisory_when_too_few_trades(monkeypatch):
    monkeypatch.setattr(mandate, "book_economics", lambda **k: _econ(5, "1.0"))
    _, advisory = mandate._economics_advisory("gemini-momentum")
    assert advisory == ""


def test_advisory_never_raises(monkeypatch):
    def _boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr(mandate, "book_economics", _boom)
    econ, advisory = mandate._economics_advisory("gemini-momentum")
    assert econ == {} and advisory == ""

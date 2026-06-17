"""S6 — cross-market backtest sweep planner (pure; no data/DB)."""

from __future__ import annotations

from helm.eval.sweep import sweep_plan


def test_crypto_plan_excludes_session_and_context_strategies():
    plan = sweep_plan(["CRYPTO"])
    strats = {s for _, s, _ in plan}
    syms = {sym for _, _, sym in plan}
    # 24/7 venue → no opening-range / gap-fade (session_required)
    assert not any("orb" in s or "gap_fade" in s for s in strats)
    # session-agnostic strategies are included
    assert any("bbands" in s or "vwap" in s for s in strats)
    # context strategy is IN-only
    assert "context_momentum" not in strats
    # only crypto symbols
    assert syms <= {"BTC", "ETH"}


def test_in_plan_includes_session_strategies():
    strats = {s for _, s, _ in sweep_plan(["IN"])}
    assert any("orb" in s for s in strats)       # NSE is sessioned
    assert any("gap_fade" in s for s in strats)


def test_us_plan_is_session_capable_usd_symbols():
    plan = sweep_plan(["US"])
    strats = {s for _, s, _ in plan}
    syms = {sym for _, _, sym in plan}
    assert any("orb" in s for s in strats)       # NYSE is sessioned → ORB allowed
    assert syms <= {"AAPL", "MSFT", "NVDA", "SPY", "QQQ"}

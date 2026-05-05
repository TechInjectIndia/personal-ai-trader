"""
Tests for the risk module.

NFR-9 requires ≥80% coverage on risk paths and ≥90% on intraday risk paths.
This file is the skeleton — actual test bodies fill in during Phase 3.
"""

from decimal import Decimal

import pytest

from helm.orchestrator.risk import RiskLimits


def test_default_limits_match_prd():
    """Default RiskLimits match the numbers in PRD v1.3 §12."""
    limits = RiskLimits()
    assert limits.max_daily_loss_pct == Decimal("0.02")
    assert limits.max_monthly_drawdown_pct == Decimal("0.10")
    assert limits.intraday_daily_loss_cap_pct == Decimal("0.01")
    assert limits.max_single_name_pct_passive == Decimal("0.07")
    assert limits.max_single_name_pct_intraday == Decimal("0.02")
    assert limits.sleeve_b_cap_pct == Decimal("0.15")


@pytest.mark.skip(reason="Phase 3 — pre_trade_check not yet implemented")
def test_pre_trade_rejects_oversize_passive_position():
    pass


@pytest.mark.skip(reason="Phase 3 — pre_trade_check not yet implemented")
def test_pre_trade_rejects_intraday_without_stoploss():
    pass


@pytest.mark.skip(reason="Phase 3 — post_trade_check not yet implemented")
def test_post_trade_triggers_sleeve_halt_on_intraday_breach():
    pass


@pytest.mark.skip(reason="Phase 3 — post_trade_check not yet implemented")
def test_post_trade_triggers_manual_hold_on_monthly_drawdown():
    pass

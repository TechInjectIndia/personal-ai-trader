"""S2 — per-market (currency-correct) risk caps. IN byte-identical; US/CRYPTO USD."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from helm.config import live_risk_limits, live_risk_limits_for


def test_in_is_byte_identical_to_live_risk_limits():
    a, b = live_risk_limits_for("IN"), live_risk_limits()
    assert a.max_position_inr == b.max_position_inr
    assert a.daily_loss_kill_inr == b.daily_loss_kill_inr
    assert a.max_open_positions == b.max_open_positions


def test_unknown_market_falls_back_to_in():
    assert live_risk_limits_for("ZZZ").max_position_inr == live_risk_limits().max_position_inr


def test_us_and_crypto_use_usd_caps():
    us = live_risk_limits_for("US")
    cr = live_risk_limits_for("CRYPTO")
    assert us.max_position_inr == Decimal("1500")     # USD per-trade cap
    assert us.daily_loss_kill_inr == Decimal("100")
    assert cr.max_position_inr == Decimal("300")
    assert cr.daily_loss_kill_inr == Decimal("50")
    # Count-based limits stay shared (currency-agnostic).
    assert us.max_open_positions == live_risk_limits().max_open_positions


@pytest.mark.skipif(os.environ.get("HELM_SEARCH_PATH") != "mm_test",
                    reason="integration test; needs the isolated mm_test schema")
def test_us_cap_blocks_in_does_not():
    from helm.data.store import conn
    from helm.orchestrator import risk

    with conn() as c:
        c.execute("DELETE FROM paper_trades")
        # ensure US wallet seeded (migrate seeds US $5000); idempotent
        c.execute("INSERT INTO wallets (market,currency,initial_capital,goal_capital) "
                  "VALUES ('US','USD',5000,10000) ON CONFLICT (market) DO NOTHING")
    # notional 20*100 = 2000 > US cap 1500 → blocked; < IN cap 15000 → allowed.
    ok_us, reason_us = risk.evaluate("AAPL", "BUY", 20, Decimal("100"), market="US")
    ok_in, _ = risk.evaluate("RELIANCE", "BUY", 20, Decimal("100"), market="IN")
    assert ok_us is False and "cap" in reason_us
    assert ok_in is True

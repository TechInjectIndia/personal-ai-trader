"""Trade-explorer pure helpers (filters, %-P&L, period cutoff, agent labels)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from helm.dashboard import trade_explorer as te

IST = ZoneInfo("Asia/Kolkata")


def test_pct_net_is_net_over_entry_notional():
    # 100 net on 10 shares @ 200 entry = 100 / 2000 = 5%
    assert te.pct_net(100, 200, 10) == 5.0
    assert te.pct_net(-50, 100, 10) == -5.0


def test_pct_net_none_when_no_notional_or_bad_input():
    assert te.pct_net(100, 0, 10) is None
    assert te.pct_net(100, 200, 0) is None
    assert te.pct_net(100, None, 10) is None
    assert te.pct_net(100, "x", 10) is None


def test_period_cutoff_today_is_ist_midnight():
    now = datetime(2026, 6, 18, 14, 30, tzinfo=IST)
    cut = te.period_cutoff("Today", now)
    assert cut == datetime(2026, 6, 18, 0, 0, tzinfo=IST)


def test_period_cutoff_rolling_window_and_all_time():
    now = datetime(2026, 6, 18, 14, 30, tzinfo=IST)
    assert te.period_cutoff("Last 7 days", now) == datetime(2026, 6, 11, 14, 30, tzinfo=IST)
    assert te.period_cutoff("All time", now) is None


def test_filter_clauses_all_agents_all_markets_is_just_closed():
    clauses, params = te.filter_clauses(te.ALL, te.ALL, None)
    assert clauses == ["pt.status = 'CLOSED'"]
    assert params == []


def test_filter_clauses_house_uses_house_clause_no_param():
    clauses, params = te.filter_clauses(te.HOUSE, te.ALL, None)
    assert any("competitor_id IS NULL" in c for c in clauses)
    assert params == []


def test_filter_clauses_specific_agent_market_and_cutoff_are_parameterized():
    cut = datetime(2026, 6, 11, tzinfo=IST)
    clauses, params = te.filter_clauses("gemini-momentum", "IN", cut)
    assert "pt.competitor_id = %s" in clauses
    assert "pt.market = %s" in clauses
    assert "pt.exit_ts >= %s" in clauses
    assert params == ["gemini-momentum", "IN", cut]


def test_agent_label_house_and_competitor():
    names = {"gemini-momentum": "Gemini (Momentum)"}
    assert te.agent_label(None, names) == "House"
    assert te.agent_label("house-claude", names) == "House"
    assert te.agent_label("gemini-momentum", names) == "Gemini (Momentum)"
    assert te.agent_label("unknown-id", names) == "unknown-id"   # falls back to id


def test_sorts_and_periods_cover_requested_options():
    assert set(te.SORTS) == {"% Net P&L", "Net P&L", "Most recent"}
    assert set(te.PERIODS) == {"Today", "Last 7 days", "Last 30 days", "All time"}

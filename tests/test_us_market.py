"""M7 — US equities enablement: registry shape, full strategy set on a
sessioned venue, and the market-timezone 'today' boundary."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from helm.markets.registry import MARKET_US
from helm.orchestrator.risk import _market_today, _market_tz
from helm.strategies.intraday.orb import OpeningRangeBreakout
from scripts.scan_signals import _strategy_allowed


def test_us_registry_shape():
    assert MARKET_US.currency == "USD"
    assert MARKET_US.fractional is False         # integer shares
    assert MARKET_US.max_hold_min is None        # sessioned, not 24/7
    assert MARKET_US.calendar.square_off_at() is not None
    assert getattr(MARKET_US.data, "suffix", None) == ""   # bare US ticker


def test_us_runs_session_strategies():
    # NYSE is sessioned, so opening-range/gap strategies are allowed (unlike crypto).
    assert _strategy_allowed(OpeningRangeBreakout(or_minutes=15), MARKET_US) is True


def test_market_tz_per_venue():
    assert _market_tz("IN") == "Asia/Kolkata"
    assert _market_tz("US") == "America/New_York"
    assert _market_tz("CRYPTO") == "UTC"


def test_market_today_in_is_ist_date():
    # IN keeps the IST trading-day boundary (byte-identical to _today_ist).
    assert _market_today("IN") == datetime.now(ZoneInfo("Asia/Kolkata")).date()


@pytest.mark.skipif(os.environ.get("HELM_SEARCH_PATH") != "mm_test",
                    reason="integration test; needs the isolated mm_test schema")
def test_us_candles_scoped_to_et_day():
    from helm.data.store import conn, todays_candles

    with conn() as c:
        c.execute("DELETE FROM candles_1m WHERE market = 'US'")
        c.execute("INSERT INTO candles_1m (market,symbol,bar_ts,open,high,low,close,tick_count) "
                  "VALUES ('US','AAPL', now(), 200,200,200,200,1)")
    rows = todays_candles("AAPL", market="US", tz="America/New_York")
    assert len(rows) >= 1
    # The IN-default query must NOT see the US candle (market isolation).
    assert todays_candles("AAPL", market="IN") == []

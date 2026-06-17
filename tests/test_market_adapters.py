"""M3 — data adapters + calendars (pure unit tests, no network)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from helm.markets.calendars import AlwaysOpen, NYSECalendar
from helm.markets.data import (
    AlpacaData,
    CCXTData,
    YFinanceNS,
    _alpaca_timeframe,
    _ccxt_timeframe,
    _parse_iso,
    _yf_interval,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def test_always_open_is_24_7_no_squareoff():
    cal = AlwaysOpen()
    # Sunday 03:00 UTC — crypto trades weekends/nights.
    sun = datetime(2026, 6, 21, 3, 0, tzinfo=UTC)
    assert cal.is_market_open(sun) is True
    assert cal.is_trading_window(sun) is True
    assert cal.square_off_at() is None
    assert cal.trading_day_key(sun).day == 21


def test_nyse_regular_session_hours():
    cal = NYSECalendar()
    wed = datetime(2026, 6, 17, 10, 0, tzinfo=ET)        # Wed mid-session
    after = datetime(2026, 6, 17, 17, 0, tzinfo=ET)      # after close
    sat = datetime(2026, 6, 20, 12, 0, tzinfo=ET)        # weekend
    assert cal.is_market_open(wed) is True
    assert cal.is_trading_window(wed) is True
    assert cal.is_market_open(after) is False
    assert cal.is_market_open(sat) is False
    assert cal.square_off_at() is not None


def test_nyse_holiday_is_closed():
    pytest.importorskip("pandas_market_calendars")
    cal = NYSECalendar()
    # 2026-12-25 (Christmas) — NYSE closed even though it's a Friday.
    xmas = datetime(2026, 12, 25, 10, 0, tzinfo=ET)
    assert cal.is_market_open(xmas) is False


def test_ccxt_symbol_mapping_and_timeframes():
    d = CCXTData()
    assert d._pair("BTC") == "BTC/USDT"
    assert d._pair("ETH/USDT") == "ETH/USDT"
    assert d.source == "ccxt"
    assert _ccxt_timeframe(1) == "1m"
    assert _ccxt_timeframe(5) == "5m"
    assert _ccxt_timeframe(60) == "1h"


def test_alpaca_and_yf_timeframes_and_iso():
    assert AlpacaData().source == "alpaca"
    assert _alpaca_timeframe(5) == "5Min"
    assert _alpaca_timeframe(60) == "1Hour"
    assert _yf_interval(1) == "1m"
    assert _yf_interval(60) == "1h"
    assert _yf_interval(1440) == "1d"
    dt = _parse_iso("2026-01-02T09:30:00Z")
    assert dt.tzinfo is not None and dt.year == 2026


def test_yfinance_ns_unchanged():
    assert YFinanceNS().source == "yfinance"
    assert YFinanceNS().suffix == ".NS"

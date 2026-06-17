"""M1 — market abstraction. Asserts the IN routing is byte-identical to the
pre-refactor inline logic (no DB / no network here)."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm import charges
from helm.config import (
    MARKET_CLOSE,
    MARKET_OPEN,
    SQUARE_OFF_AT,
    TRADING_END,
    TRADING_START,
    WATCHLIST,
)
from helm.markets import MARKET_IN, all_markets, enabled_markets, get_market
from helm.markets.calendars import NSECalendar

IST = ZoneInfo("Asia/Kolkata")


def _old_is_market_open(now: datetime) -> bool:
    """The exact pre-refactor poll/manage gate."""
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _old_is_trading_window(now: datetime) -> bool:
    """The exact pre-refactor scan_signals gate."""
    if now.weekday() >= 5:
        return False
    return TRADING_START <= now.time() <= TRADING_END


def _week_of_timestamps() -> list[datetime]:
    # Mon 2026-06-15 .. Sun 2026-06-21, every 5 minutes across the day.
    out = []
    for day in range(15, 22):
        for hh in range(0, 24):
            for mm in (0, 5, 14, 15, 16, 30, 44, 45, 59):
                out.append(datetime(2026, 6, day, hh, mm, tzinfo=IST))
    return out


def test_nse_calendar_is_byte_identical_to_old_gates():
    cal = NSECalendar()
    for ts in _week_of_timestamps():
        assert cal.is_market_open(ts) == _old_is_market_open(ts), ts
        assert cal.is_trading_window(ts) == _old_is_trading_window(ts), ts


def test_nse_calendar_square_off_and_tz():
    cal = NSECalendar()
    assert cal.square_off_at() == SQUARE_OFF_AT
    assert cal.tz == IST
    # trading_day_key is the IST date even for a UTC-expressed instant.
    utc = ZoneInfo("UTC")
    # 2026-06-16 20:00 UTC == 2026-06-17 01:30 IST → IST date is the 17th.
    assert cal.trading_day_key(datetime(2026, 6, 16, 20, 0, tzinfo=utc)).day == 17


def test_zerodha_costs_delegate_to_charges():
    z = MARKET_IN.costs
    for side in ("BUY", "SELL"):
        qty, entry, exit_ = Decimal("10"), Decimal("100"), Decimal("101")
        assert z.round_trip_charges(side, qty, entry, exit_) == charges.round_trip_charges(
            side, qty, entry, exit_
        )
        assert (
            z.round_trip_breakdown(side, qty, entry, exit_).as_dict()
            == charges.round_trip_breakdown(side, qty, entry, exit_).as_dict()
        )


def test_registry_defaults_to_in_only():
    assert [m.key for m in enabled_markets()] == ["IN"]
    assert get_market("IN") is MARKET_IN
    assert "IN" in all_markets()


def test_market_in_shape():
    assert MARKET_IN.currency == "INR"
    assert MARKET_IN.fractional is False
    assert MARKET_IN.watchlist == tuple(WATCHLIST)
    assert MARKET_IN.max_hold_min is None
    assert getattr(MARKET_IN.data, "source", None) == "yfinance"
    assert isinstance(MARKET_IN.calendar.square_off_at(), time)

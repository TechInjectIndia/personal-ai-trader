"""Calendar session_hours()/tz_label — drives the Markets page 'Hours' line."""

from __future__ import annotations

from datetime import time

from helm.config import MARKET_CLOSE, MARKET_OPEN
from helm.markets.calendars import AlwaysOpen, NSECalendar, NYSECalendar


def test_nse_session_hours_match_config_and_label():
    cal = NSECalendar()
    assert cal.tz_label == "IST"
    assert cal.session_hours() == (MARKET_OPEN, MARKET_CLOSE)


def test_nyse_session_hours_regular_session_and_label():
    cal = NYSECalendar()
    assert cal.tz_label == "ET"
    assert cal.session_hours() == (time(9, 30), time(16, 0))


def test_crypto_is_24x7_no_session_hours():
    cal = AlwaysOpen()
    assert cal.tz_label == "UTC"
    assert cal.session_hours() is None
    assert cal.square_off_at() is None   # 24/7 → no EOD flatten

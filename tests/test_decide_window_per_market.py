"""decide_signals._within_window — market-aware gating (IN byte-identical)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import scripts.decide_signals as ds

IST = ZoneInfo("Asia/Kolkata")


def test_in_window_weekday_inside_and_outside():
    # 2026-06-18 is a Thursday; IN window is 09:30–14:45 IST.
    assert ds._within_window(datetime(2026, 6, 18, 11, 0, tzinfo=IST), "IN") is True
    assert ds._within_window(datetime(2026, 6, 18, 15, 0, tzinfo=IST), "IN") is False
    assert ds._within_window(datetime(2026, 6, 18, 9, 0, tzinfo=IST), "IN") is False


def test_in_window_weekend_false():
    # 2026-06-20 is a Saturday.
    assert ds._within_window(datetime(2026, 6, 20, 11, 0, tzinfo=IST), "IN") is False


def test_in_never_touches_market_registry(monkeypatch):
    # IN must stay byte-identical and independent of the market registry.
    def boom(_key):
        raise AssertionError("IN path must not call get_market")

    monkeypatch.setattr(ds, "get_market", boom)
    assert ds._within_window(datetime(2026, 6, 18, 11, 0, tzinfo=IST), "IN") is True


def test_non_in_delegates_to_that_markets_calendar(monkeypatch):
    seen = {}

    class _Cal:
        def is_trading_window(self, now):
            seen["now"] = now
            return "DELEGATED"

    class _Mkt:
        calendar = _Cal()

    monkeypatch.setattr(ds, "get_market", lambda _k: _Mkt())
    now = datetime(2026, 6, 18, 3, 0, tzinfo=IST)   # 03:00 IST — IN closed
    assert ds._within_window(now, "CRYPTO") == "DELEGATED"
    assert seen["now"] is now

"""#5 — OpenBB data adapter (pure parser + registry wiring).

The adapter itself is fail-soft and lazy-imports `openbb` (an optional extra not
installed in the live venv), so these tests cover the pure parser + the config-
driven routing WITHOUT touching the network or requiring openbb. A live fetch
test is intentionally skipped unless openbb is present.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from helm.markets.data import OpenBBData, _obb_to_candles, _openbb_interval

UTC = timezone.utc


def test_openbb_interval_mapping():
    assert _openbb_interval(1) == "1m"
    assert _openbb_interval(5) == "5m"
    assert _openbb_interval(60) == "1h"
    assert _openbb_interval(120) == "2h"
    assert _openbb_interval(1440) == "1d"


def test_obb_to_candles_empty_is_safe():
    assert _obb_to_candles(None) == []


def test_obb_to_candles_parses_lowercase_date_indexed_frame():
    pd = pytest.importorskip("pandas")
    idx = pd.to_datetime(["2026-06-01 09:30:00", "2026-06-01 09:31:00"])
    df = pd.DataFrame(
        {"open": [100.0, 101.0], "high": [101.5, 102.0], "low": [99.5, 100.5],
         "close": [101.0, 101.8], "volume": [1000, 1200]},
        index=idx,
    )
    candles = _obb_to_candles(df)
    assert len(candles) == 2
    first = candles[0]
    assert {"bar_ts", "open", "high", "low", "close", "tick_count"} <= set(first)
    assert str(first["close"]) == "101.0"
    assert first["tick_count"] == 1000
    assert first["bar_ts"].tzinfo is not None  # naive index → stamped UTC


def test_obb_to_candles_handles_daily_plain_date_index():
    # OpenBB returns a plain `datetime.date` index for DAILY bars (not a pandas
    # Timestamp) — date.replace(tzinfo=...) is a TypeError. Regression for the
    # parse crash found in live validation (daily fetch returned 0 candles).
    import datetime as dt

    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5],
         "volume": [10]},
        index=[dt.date(2026, 6, 1)],
    )
    candles = _obb_to_candles(df)
    assert len(candles) == 1
    assert candles[0]["bar_ts"] == dt.datetime(2026, 6, 1, tzinfo=UTC)
    assert str(candles[0]["close"]) == "100.5"


def test_obb_to_candles_accepts_date_column():
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        {"date": pd.to_datetime(["2026-06-01"]), "open": [10.0], "high": [11.0],
         "low": [9.0], "close": [10.5], "volume": [5]},
    )
    candles = _obb_to_candles(df)
    assert len(candles) == 1 and str(candles[0]["open"]) == "10.0"


def test_adapter_fails_soft_without_openbb_installed():
    # openbb isn't in the venv → lazy import raises → historical/last_price swallow.
    a = OpenBBData(asset="equity", suffix=".NS")
    assert a.source == "openbb"
    assert a.historical("RELIANCE", 1, datetime.now(UTC), datetime.now(UTC)) == []
    assert a.last_price("RELIANCE") is None


def test_registry_routes_to_openbb_when_configured(monkeypatch):
    import importlib

    import helm.config as config

    monkeypatch.setitem(config.MARKET_DATA_PROVIDER, "US", "openbb")
    registry = importlib.reload(importlib.import_module("helm.markets.registry"))
    try:
        assert registry.get_market("US").data.source == "openbb"
        assert registry.get_market("IN").data.source == "yfinance"  # default unchanged
    finally:
        monkeypatch.setitem(config.MARKET_DATA_PROVIDER, "US", "yfinance")
        importlib.reload(registry)  # restore module-level markets for other tests

"""Regression: yfinance MultiIndex frame parsing in _df_to_candles.

yfinance 1.3 returns MultiIndex columns ('Close','AAPL') even for a single
ticker; `df['Close']` then yields a 1-col DataFrame, and the old parser threw
`ValueError: truth value of a Series is ambiguous` (swallowed by the adapter's
bare except → empty US/IN backtests). These pin the squeeze fix."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from helm.markets.data import _df_to_candles


def test_multiindex_single_ticker_frame():
    idx = pd.to_datetime(["2026-06-17 09:30", "2026-06-17 09:35"], utc=True)
    cols = pd.MultiIndex.from_tuples(
        [("Open", "AAPL"), ("High", "AAPL"), ("Low", "AAPL"),
         ("Close", "AAPL"), ("Volume", "AAPL")])
    df = pd.DataFrame([[100, 101, 99, 100.5, 1000],
                       [100.5, 102, 100, 101.5, 1200]], index=idx, columns=cols)
    out = _df_to_candles(df)
    assert len(out) == 2
    assert out[0]["open"] == Decimal("100") and out[0]["close"] == Decimal("100.5")
    assert out[1]["tick_count"] == 1200
    assert out[0]["bar_ts"].tzinfo is not None


def test_flat_columns_frame():
    idx = pd.to_datetime(["2026-06-17 09:30"], utc=True)
    df = pd.DataFrame([[100, 101, 99, 100.5, 1000]], index=idx,
                      columns=["Open", "High", "Low", "Close", "Volume"])
    out = _df_to_candles(df)
    assert len(out) == 1 and out[0]["close"] == Decimal("100.5")


def test_empty_frames():
    assert _df_to_candles(None) == []
    assert _df_to_candles(pd.DataFrame()) == []

"""
Unit tests for the bbands_zscore_20 mean-reversion strategy.

Mirrors the synthetic-candle approach of tests/test_pipeline_smoke.py
(_build_orb_breakout_candles): hand-built 1-min OHLC dicts with Decimal values
and tz-aware bar_ts, no DB access. Two cases:

  (a) a clean lower-band pierce fires a BUY with correct Signal fields;
  (b) the bar AFTER the trigger (price still oversold) does NOT re-emit —
      the edge-trigger / fire-once guard holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.strategies.intraday.mean_reversion import MeanReversion

IST = ZoneInfo("Asia/Kolkata")


def _bar(ts: datetime, close: Decimal, tick_count: int = 5) -> dict:
    """A 1-min bar shaped like production candles (no 'symbol'/'volume' key)."""
    # OHLC kept self-consistent; the strategy only reads close.
    return {
        "bar_ts": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "tick_count": tick_count,
    }


def _build_oversold_pierce() -> list[dict]:
    """22 bars: a steady ~oscillating tape near 100 with real volatility, then a
    final bar that dips to a z of ~-2.3 (oversold, but not so far past -2 that
    the mean-3sigma structural stop would sit above entry) while the prior bar's
    z was still above -2. Base ts at 10:00 IST (inside the 09:45-14:45 gate).
    """
    base = datetime(2026, 5, 19, 10, 0, tzinfo=IST)
    # 21 prior bars oscillating around 100 (gives a non-trivial stdev and a
    # mean ~100, with the drift guard satisfied since there's no net direction).
    prices = [
        "100.00", "100.40", "99.70", "100.30", "99.80",
        "100.20", "99.90", "100.10", "100.00", "100.30",
        "99.70", "100.20", "99.80", "100.10", "99.90",
        "100.20", "99.80", "100.10", "99.90", "100.00",
        "99.95",  # 21st bar (candles[-2]); z here must be > -2
    ]
    candles = [
        _bar(base + timedelta(minutes=i), Decimal(p))
        for i, p in enumerate(prices)
    ]
    # 22nd bar: dip to z ~= -2.3 — oversold, stop stays below entry.
    candles.append(_bar(base + timedelta(minutes=len(prices)), Decimal("99.45")))
    return candles


def test_fires_on_lower_band_pierce() -> None:
    strat = MeanReversion()
    candles = _build_oversold_pierce()
    sig = strat.scan("RELIANCE", candles)

    assert sig is not None, "expected a BUY on the lower-band pierce"
    assert sig.strategy == "bbands_zscore_20"
    assert sig.symbol == "RELIANCE"
    assert sig.side == "BUY"
    assert sig.asof == candles[-1]["bar_ts"]

    # Prices are Decimals.
    assert isinstance(sig.entry_price, Decimal)
    assert isinstance(sig.stop_loss, Decimal)
    assert isinstance(sig.target, Decimal)

    # Entry is the latest (oversold) close; stop is below entry; target above.
    assert sig.entry_price == Decimal("99.45")
    assert sig.stop_loss < sig.entry_price
    assert sig.target > sig.entry_price

    # Reward-to-risk honours the 1.5 floor.
    risk = sig.entry_price - sig.stop_loss
    reward = sig.target - sig.entry_price
    assert reward / risk >= Decimal("1.5")

    # Payload carries the band/stat context as strings.
    assert sig.payload["window"] == "20"
    assert sig.payload["k"] == "2.0"
    assert sig.payload["stop_basis"] == "mean-3sigma"
    assert sig.payload["target_basis"] == "trailing-20-mean"


def test_does_not_reemit_on_bar_after_trigger() -> None:
    """The bar after the pierce: price is STILL oversold, so z_prior is already
    <= -2 and the edge trigger fails — scan must return None (no re-emit)."""
    strat = MeanReversion()
    candles = _build_oversold_pierce()

    # Confirm the trigger bar fires first.
    assert strat.scan("RELIANCE", candles) is not None

    # Append one more equally-low bar: now candles[-2] (the old trigger bar) is
    # itself oversold (z_prior <= -2), so the fire-once guard blocks re-emit.
    next_ts = candles[-1]["bar_ts"] + timedelta(minutes=1)
    candles.append(_bar(next_ts, Decimal("99.45")))

    assert strat.scan("RELIANCE", candles) is None


def test_no_signal_with_insufficient_bars() -> None:
    strat = MeanReversion()
    base = datetime(2026, 5, 19, 10, 0, tzinfo=IST)
    candles = [_bar(base + timedelta(minutes=i), Decimal("100.00")) for i in range(10)]
    assert strat.scan("RELIANCE", candles) is None


def test_outside_time_window_returns_none() -> None:
    """Same pierce shape but anchored so the trigger bar lands after 14:45 IST."""
    strat = MeanReversion()
    candles = _build_oversold_pierce()
    shift = datetime(2026, 5, 19, 15, 0, tzinfo=IST) - candles[-1]["bar_ts"]
    for c in candles:
        c["bar_ts"] = c["bar_ts"] + shift
    assert strat.scan("RELIANCE", candles) is None

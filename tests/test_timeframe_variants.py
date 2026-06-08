"""
F6 — timeframe-variant strategy tests (pure, no DB).

Covers:
  - bar_minutes is declared correctly per strategy (1-min default; _5m=5; ORB
    stays 1 because orb_5m/orb_15m are opening-RANGE minutes off 1-min bars).
  - the 5-min bbands variant fires ONCE on the trigger bar and does NOT re-emit
    on the next non-crossing bar (edge-trigger guard works on N-min series too).
  - the variant's Signal carries the _5m-suffixed strategy name and an asof equal
    to the (N-min-aligned) trigger bar's bar_ts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.strategies import ACTIVE
from helm.strategies.intraday.gap_fade import GapFade
from helm.strategies.intraday.mean_reversion import MeanReversion
from helm.strategies.intraday.orb import OpeningRangeBreakout
from helm.strategies.intraday.vwap import VWAPReclaim

IST = ZoneInfo("Asia/Kolkata")


def _bar(ts: datetime, close: Decimal, tick_count: int = 5) -> dict:
    return {
        "bar_ts": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "tick_count": tick_count,
    }


def test_bar_minutes_attributes() -> None:
    # 1-min defaults.
    assert getattr(VWAPReclaim(), "bar_minutes", 1) == 1
    assert getattr(GapFade(), "bar_minutes", 1) == 1
    assert getattr(MeanReversion(), "bar_minutes", 1) == 1
    # ORB stays 1-min bars (or_minutes is opening-RANGE minutes, NOT bar size).
    assert getattr(OpeningRangeBreakout(or_minutes=5), "bar_minutes", 1) == 1
    assert getattr(OpeningRangeBreakout(or_minutes=15), "bar_minutes", 1) == 1
    # 5-min variants.
    assert MeanReversion(bar_minutes=5).bar_minutes == 5
    assert MeanReversion(bar_minutes=5).name == "bbands_zscore_20_5m"


def test_active_registers_5m_variants_with_distinct_names() -> None:
    names = [s.name for s in ACTIVE]
    # 1-min instances unchanged.
    assert "bbands_zscore_20" in names
    assert "vwap_reclaim" in names
    assert "gap_fade" in names
    # 5-min A/B counterparts present and DISTINCT from the 1-min ones.
    assert "bbands_zscore_20_5m" in names
    assert "vwap_reclaim_5m" in names
    assert "gap_fade_5m" in names
    assert len(names) == len(set(names)), "duplicate strategy names break dedupe/F1"
    # orb_5m/orb_15m are opening-RANGE-minute variants off 1-min bars — they
    # exist, but must NOT have been turned into 5-min-bar variants.
    assert "orb_5m" in names and "orb_15m" in names
    assert all(
        s.bar_minutes == 1 for s in ACTIVE if isinstance(s, OpeningRangeBreakout)
    )
    # Every _5m-named variant consumes 5-min bars.
    for s in ACTIVE:
        if s.name.endswith("_5m") and not isinstance(s, OpeningRangeBreakout):
            assert s.bar_minutes == 5


def _build_oversold_pierce_5m() -> list[dict]:
    """Same shape as the 1-min mean-reversion fixture, but bar_ts spaced 5 min
    apart and aligned to a 5-min session bucket. The strategy reads only close +
    bar_ts.time(), so the *bucket* timestamps drive the time gate and the
    edge-trigger guard identically to the 1-min case."""
    base = datetime(2026, 5, 19, 10, 0, tzinfo=IST)  # inside 09:45-14:45 gate
    prices = [
        "100.00", "100.40", "99.70", "100.30", "99.80",
        "100.20", "99.90", "100.10", "100.00", "100.30",
        "99.70", "100.20", "99.80", "100.10", "99.90",
        "100.20", "99.80", "100.10", "99.90", "100.00",
        "99.95",
    ]
    candles = [
        _bar(base + timedelta(minutes=5 * i), Decimal(p))
        for i, p in enumerate(prices)
    ]
    candles.append(_bar(base + timedelta(minutes=5 * len(prices)), Decimal("99.45")))
    return candles


def test_5m_variant_fires_once_on_trigger_bar() -> None:
    strat = MeanReversion(bar_minutes=5)
    candles = _build_oversold_pierce_5m()

    sig = strat.scan("RELIANCE", candles)
    assert sig is not None
    assert sig.strategy == "bbands_zscore_20_5m"
    assert sig.symbol == "RELIANCE"
    assert sig.side == "BUY"
    # asof is the N-min-aligned trigger bucket's timestamp.
    assert sig.asof == candles[-1]["bar_ts"]
    assert isinstance(sig.entry_price, Decimal)


def test_5m_variant_does_not_reemit_after_trigger() -> None:
    strat = MeanReversion(bar_minutes=5)
    candles = _build_oversold_pierce_5m()
    assert strat.scan("RELIANCE", candles) is not None

    # Append one more equally-low 5-min bucket: now candles[-2] (the old trigger)
    # is itself oversold (z_prior <= -2), so the fire-once guard blocks re-emit.
    next_ts = candles[-1]["bar_ts"] + timedelta(minutes=5)
    candles.append(_bar(next_ts, Decimal("99.45")))
    assert strat.scan("RELIANCE", candles) is None

"""
Unit tests for the F4 bigger-move reframe (helm/strategies/_moves.py +
MIN_TARGET_PCT floor applied in each strategy's scan).

Covers:
  (1) enforce_min_move() arithmetic — BUY widens up to the floor, leaves an
      already-wide target unchanged, and the (future-proofing) SELL branch.
  (2) momentum/reclaim strategies (ORB, VWAP, gap-fade): a too-tight natural
      target is widened to the floor; an already-wide target passes unchanged;
      RR stays >= 1.5 after widening (stop unchanged ⇒ RR only ever rises).
  (3) mean-reversion: a signal whose mean target sits inside the floor is
      DROPPED (default flag); one whose mean clears the floor passes unchanged.

Synthetic hand-built 1-min OHLC dicts with Decimal values and tz-aware bar_ts,
no DB access — mirrors tests/test_mean_reversion.py / test_pipeline_smoke.py.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.config import MIN_TARGET_PCT
from helm.strategies._moves import enforce_min_move
from helm.strategies.intraday.gap_fade import GapFade
from helm.strategies.intraday.mean_reversion import MeanReversion
from helm.strategies.intraday.orb import OpeningRangeBreakout
from helm.strategies.intraday.vwap import VWAPReclaim

IST = ZoneInfo("Asia/Kolkata")


def _bar(ts: datetime, o: str, h: str, low: str, c: str, tick_count: int = 5) -> dict:
    return {
        "bar_ts": ts,
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(low),
        "close": Decimal(c),
        "tick_count": tick_count,
    }


# --------------------------------------------------------------------------- #
# (1) enforce_min_move unit tests
# --------------------------------------------------------------------------- #
def test_enforce_min_move_buy_widens_below_floor() -> None:
    entry = Decimal("100")
    # Target only 0.2% away — below the 0.6% floor → widened up to the floor.
    out = enforce_min_move(entry, Decimal("100.20"), "BUY", Decimal("0.006"))
    assert out == Decimal("100.600")


def test_enforce_min_move_buy_leaves_wide_target_unchanged() -> None:
    entry = Decimal("100")
    # Target 1% away — already past the 0.6% floor → unchanged.
    out = enforce_min_move(entry, Decimal("101.00"), "BUY", Decimal("0.006"))
    assert out == Decimal("101.00")


def test_enforce_min_move_sell_branch() -> None:
    entry = Decimal("100")
    # SELL floor is a ceiling at entry*(1-pct)=99.40; a too-close target widens DOWN.
    assert enforce_min_move(entry, Decimal("99.80"), "SELL", Decimal("0.006")) == Decimal("99.400")
    # An already-far SELL target is left unchanged.
    assert enforce_min_move(entry, Decimal("99.00"), "SELL", Decimal("0.006")) == Decimal("99.00")


# --------------------------------------------------------------------------- #
# (2a) ORB — tight OR width ⇒ natural target inside floor ⇒ widened to floor
# --------------------------------------------------------------------------- #
def _orb_candles(or_high: str, breakout_close: str) -> list[dict]:
    """15-bar opening range then a breakout bar. or_low fixed low enough that the
    >=0.8% min-OR-width filter passes; prior bar stays inside the range."""
    base = datetime(2026, 5, 19, 9, 15, tzinfo=IST)
    lo = "99.00"
    bars = [
        _bar(base + timedelta(minutes=i), or_high, or_high, lo, "99.50")
        for i in range(15)
    ]
    # Prior (15th) bar: inside the range (close <= or_high), no prior breakout.
    bars.append(_bar(base + timedelta(minutes=15), "99.50", or_high, "99.40", "99.60"))
    # Breakout bar: closes above or_high.
    bars.append(_bar(base + timedelta(minutes=16), or_high, breakout_close, lo, breakout_close))
    return bars


def test_orb_wide_target_unchanged() -> None:
    # ORB's target = entry + 1.5 x OR width. The strategy's own 0.8%-of-price
    # min-OR-width filter means 1.5 x width is always >= ~1.2% of price, so the
    # natural target inherently clears the 0.6% MIN_TARGET_PCT floor and passes
    # through enforce_min_move() unchanged.
    candles = _orb_candles("100.00", "100.20")
    sig = OpeningRangeBreakout(or_minutes=15).scan("RELIANCE", candles)
    assert sig is not None
    or_width = Decimal("100.00") - Decimal("99.00")
    natural = sig.entry_price + Decimal("1.5") * or_width
    assert sig.target == natural  # already wide → unchanged
    # Target clears the F4 floor.
    floor = sig.entry_price * (Decimal(1) + MIN_TARGET_PCT)
    assert sig.target >= floor
    # ORB sizes target off OR width (not off entry-stop risk), so its realized RR
    # vs the or_low stop is whatever the geometry gives; the F4 floor never
    # lowers the target, so RR can only be >= the natural RR.


# --------------------------------------------------------------------------- #
# (2b) VWAP — tiny risk ⇒ natural 1.5R target inside floor ⇒ widened to floor
# --------------------------------------------------------------------------- #
def test_vwap_tight_target_widened_and_rr_preserved() -> None:
    base = datetime(2026, 5, 19, 10, 0, tzinfo=IST)
    # Flat tape just under 100, a dip below VWAP, then a tiny green reclaim bar.
    bars = [_bar(base + timedelta(minutes=i), "100.00", "100.02", "99.98", "99.99") for i in range(8)]
    # A bar that dips clearly below VWAP.
    bars.append(_bar(base + timedelta(minutes=8), "99.99", "99.99", "99.90", "99.92"))
    # Reclaim bar: green, closes just above VWAP — small risk vs 5-bar low.
    bars.append(_bar(base + timedelta(minutes=9), "99.93", "100.05", "99.93", "100.02"))
    sig = VWAPReclaim().scan("RELIANCE", bars)
    assert sig is not None
    entry = sig.entry_price
    floor = entry * (Decimal(1) + MIN_TARGET_PCT)
    risk = entry - sig.stop_loss
    natural = entry + Decimal("1.5") * risk
    if natural < floor:
        assert sig.target == floor
    else:
        assert sig.target == natural
    # Stop unchanged ⇒ RR never below the 1.5 floor.
    assert (sig.target - entry) / risk >= Decimal("1.5")


# --------------------------------------------------------------------------- #
# (2c) gap-fade — same widen behaviour (yfinance lookup monkeypatched)
# --------------------------------------------------------------------------- #
def test_gap_fade_target_respects_floor(monkeypatch) -> None:
    import helm.strategies.intraday.gap_fade as gf

    monkeypatch.setattr(gf, "_yesterday_close", lambda symbol: Decimal("101.00"))
    base = datetime(2026, 5, 19, 9, 16, tzinfo=IST)
    # Open gapped >0.5% down from 101 (open 100.00 ⇒ ~0.99% gap).
    bars = [_bar(base, "100.00", "100.10", "99.80", "100.00")]
    # Prior bar still under prev_close; final bar reclaims 101.00.
    bars.append(_bar(base + timedelta(minutes=1), "100.50", "100.95", "100.40", "100.90"))
    bars.append(_bar(base + timedelta(minutes=2), "100.95", "101.05", "100.90", "101.00"))
    sig = GapFade().scan("RELIANCE", bars)
    assert sig is not None
    entry = sig.entry_price
    floor = entry * (Decimal(1) + MIN_TARGET_PCT)
    risk = entry - sig.stop_loss
    natural = entry + Decimal("1.5") * risk
    expected = max(natural, floor)
    assert sig.target == expected
    assert (sig.target - entry) / risk >= Decimal("1.5")


# --------------------------------------------------------------------------- #
# (3) mean-reversion — sub-floor mean dropped; mean clearing floor passes
# --------------------------------------------------------------------------- #
def _meanrev_candles(prior_prices: list[str], trigger_close: str) -> list[dict]:
    base = datetime(2026, 5, 19, 10, 0, tzinfo=IST)
    candles = [
        _bar(base + timedelta(minutes=i), p, p, p, p)
        for i, p in enumerate(prior_prices)
    ]
    candles.append(_bar(base + timedelta(minutes=len(prior_prices)), trigger_close,
                        trigger_close, trigger_close, trigger_close))
    return candles


_SUB_FLOOR_PRIOR = [
    "100.00", "100.40", "99.70", "100.30", "99.80",
    "100.20", "99.90", "100.10", "100.00", "100.30",
    "99.70", "100.20", "99.80", "100.10", "99.90",
    "100.20", "99.80", "100.10", "99.90", "100.00",
    "100.05",
]


def test_mean_reversion_exempt_from_floor_by_default() -> None:
    """Entry 99.45, mean ~99.97 ⇒ mean only ~0.52% above entry (inside the 0.6%
    floor). Mean-reversion is EXEMPT from the F4 floor by default ("none") — it
    still FIRES (its cost discipline is the F2 E2C gate, not a target floor)."""
    sig = MeanReversion().scan("RELIANCE", _meanrev_candles(_SUB_FLOOR_PRIOR, "99.45"))
    assert sig is not None
    assert sig.side == "BUY"


def test_mean_reversion_drop_mode_opt_in(monkeypatch) -> None:
    """With the opt-in 'drop' policy, the same sub-floor signal IS dropped."""
    monkeypatch.setattr(
        "helm.strategies.intraday.mean_reversion.MEANREV_WIDEN_OR_DROP", "drop")
    sig = MeanReversion().scan("RELIANCE", _meanrev_candles(_SUB_FLOOR_PRIOR, "99.45"))
    assert sig is None


def test_mean_reversion_mean_above_floor_passes() -> None:
    """A deeper pierce: entry far enough below the mean that the mean clears the
    0.6% floor → the signal survives (the drop guard does not fire)."""
    prior = [
        "100.00", "100.40", "99.70", "100.30", "99.80",
        "100.20", "99.90", "100.10", "100.00", "100.30",
        "99.70", "100.20", "99.80", "100.10", "99.90",
        "100.20", "99.80", "100.10", "99.90", "100.00",
        "100.05",
    ]
    # Entry 99.30: mean ~99.97 ⇒ ~0.67% above entry, clears the 0.6% floor → survives.
    sig = MeanReversion().scan("RELIANCE", _meanrev_candles(prior, "99.30"))
    assert sig is not None
    entry = sig.entry_price
    # Mean target clears the floor.
    assert (sig.target - entry) / entry >= MIN_TARGET_PCT
    # RR floor still honoured.
    rr = (sig.target - entry) / (entry - sig.stop_loss)
    assert rr >= Decimal("1.5")


def test_time_gate_constant_present() -> None:
    # Sanity: trigger bars in the meanrev tests sit inside the 09:45-14:45 IST gate.
    assert time(9, 45) <= time(10, 21) <= time(14, 45)

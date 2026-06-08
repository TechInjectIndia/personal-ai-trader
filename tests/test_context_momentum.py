"""F7-P3c — ContextMomentum strategy (pure fn of candles + injected context)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.config import CONTEXT_SIGNAL_THRESHOLD
from helm.strategies import ACTIVE
from helm.strategies.intraday.context_momentum import ContextMomentum

IST = ZoneInfo("Asia/Kolkata")


def _candles(closes: list[str]) -> list[dict]:
    base = datetime(2026, 5, 19, 10, 0, tzinfo=IST)
    return [{"bar_ts": base + timedelta(minutes=i), "open": c, "high": c,
             "low": c, "close": c, "tick_count": 5} for i, c in enumerate(closes)]


def test_no_context_no_signal():
    s = ContextMomentum()  # self.context defaults None
    assert s.scan("RELIANCE", _candles(["100", "101"])) is None


def test_bullish_context_and_price_confirm_fires():
    s = ContextMomentum()
    s.context = CONTEXT_SIGNAL_THRESHOLD + Decimal("0.2")
    sig = s.scan("RELIANCE", _candles(["100.00", "101.00"]))  # price up
    assert sig is not None and sig.side == "BUY"
    assert sig.entry_price == Decimal("101.00")
    assert sig.target > sig.entry_price > sig.stop_loss
    assert (sig.target - sig.entry_price) / sig.entry_price >= Decimal("0.006")  # clears F4 floor


def test_bullish_context_but_price_fades_no_signal():
    s = ContextMomentum()
    s.context = Decimal("0.9")
    assert s.scan("RELIANCE", _candles(["101.00", "100.00"])) is None  # price down


def test_weak_context_no_signal():
    s = ContextMomentum()
    s.context = CONTEXT_SIGNAL_THRESHOLD - Decimal("0.1")
    assert s.scan("RELIANCE", _candles(["100.00", "101.00"])) is None


def test_registered_in_active_and_requires_context():
    ctx = [s for s in ACTIVE if getattr(s, "requires_context", False)]
    assert any(s.name == "context_momentum" for s in ctx)
    # default config: P3c is OFF, so scan_signals will skip these entirely
    from helm.config import CONTEXT_SIGNALS_ENABLED
    assert CONTEXT_SIGNALS_ENABLED is False

"""M5 — forward backtester. Deterministic, DB-free (candles injected, tunable
patched), proves next-bar-open fill / no look-ahead, the F2 gate, and the
crypto 24/7 time-stop."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from helm.eval.forward import backtest
from helm.markets import MARKET_IN
from helm.markets.base import Market
from helm.markets.calendars import AlwaysOpen
from helm.markets.costs import CryptoBpsCosts
from helm.markets.data import CCXTData
from helm.strategies.base import Signal

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


@pytest.fixture(autouse=True)
def _fixed_min_e2c(monkeypatch):
    # Pin MIN_EDGE_TO_COST so the backtest never touches the live settings DB.
    monkeypatch.setattr("helm.eval.forward.live_tunable", lambda name: Decimal("3.0"))


class FireOnce:
    """Fires a single BUY exactly when the latest bar is index `at`."""

    bar_minutes = 1
    requires_context = False

    def __init__(self, at: int, target_delta: str = "5", stop_delta: str = "3"):
        self.at = at
        self.name = "fireonce"
        self.td = Decimal(target_delta)
        self.sd = Decimal(stop_delta)

    def scan(self, symbol, candles):
        if len(candles) - 1 != self.at:
            return None
        last = candles[-1]
        entry = Decimal(str(last["close"]))
        return Signal(strategy=self.name, symbol=symbol, asof=last["bar_ts"], side="BUY",
                      entry_price=entry, stop_loss=entry - self.sd, target=entry + self.td,
                      rationale="t", payload={})


def _candles(opens, closes, tz=IST, base_hour=10):
    out = []
    for i, (o, c) in enumerate(zip(opens, closes)):
        out.append({
            "bar_ts": datetime(2026, 6, 17, base_hour, i, tzinfo=tz),
            "open": Decimal(str(o)), "high": Decimal(str(max(o, c))),
            "low": Decimal(str(min(o, c))), "close": Decimal(str(c)), "tick_count": 1,
        })
    return out


def test_next_bar_open_fill_and_target_exit():
    # close[3]=100 (signal), open[4]=101 (the realistic fill), price rises to 105.
    opens = [100, 100, 100, 100, 101, 102, 103, 104, 105, 106]
    closes = [100, 100, 100, 100, 100, 102, 103, 104, 105, 106]
    rep = backtest(FireOnce(at=3), MARKET_IN, "RELIANCE",
                   datetime(2026, 6, 17, tzinfo=IST), datetime(2026, 6, 17, 23, tzinfo=IST),
                   base_cap=Decimal("15000"), candles=_candles(opens, closes))
    assert rep.n_signals == 1
    assert rep.n_trades == 1
    assert rep.metrics["closed"] == 1
    # Fill at open[4]=101 → qty=floor(15000/101)=148; gross=(105-101)*148=592.
    # (Proves the fill is the NEXT bar's open, not close[3]=100 which would be 750.)
    assert rep.metrics["gross"] == 592.0


def test_deterministic_same_inputs_same_metrics():
    opens = [100, 100, 100, 100, 101, 102, 103, 104, 105, 106]
    closes = [100, 100, 100, 100, 100, 102, 103, 104, 105, 106]
    candles = _candles(opens, closes)
    a = backtest(FireOnce(at=3), MARKET_IN, "RELIANCE",
                 datetime(2026, 6, 17, tzinfo=IST), datetime(2026, 6, 17, 23, tzinfo=IST),
                 base_cap=Decimal("15000"), candles=candles)
    b = backtest(FireOnce(at=3), MARKET_IN, "RELIANCE",
                 datetime(2026, 6, 17, tzinfo=IST), datetime(2026, 6, 17, 23, tzinfo=IST),
                 base_cap=Decimal("15000"), candles=candles)
    assert a.metrics == b.metrics


def test_f2_gate_skips_thin_target():
    # Flat 100s; target only +0.20 → reward 30 vs ~16 cost → E2C < 3 → skipped.
    flat = [100] * 10
    rep = backtest(FireOnce(at=3, target_delta="0.20"), MARKET_IN, "RELIANCE",
                   datetime(2026, 6, 17, tzinfo=IST), datetime(2026, 6, 17, 23, tzinfo=IST),
                   base_cap=Decimal("15000"), candles=_candles(flat, flat))
    assert rep.n_signals == 1
    assert rep.n_trades == 0     # gated out before any trade


def test_crypto_time_stop_forces_close():
    crypto = Market(key="XC", name="crypto", data=CCXTData(), calendar=AlwaysOpen(),
                    costs=CryptoBpsCosts(), currency="USD", fractional=True,
                    watchlist=("BTC",), max_hold_min=5)
    flat = [100] * 10   # never hits the far target/stop → time-stopped
    rep = backtest(FireOnce(at=3, target_delta="50", stop_delta="50"), crypto, "BTC",
                   datetime(2026, 6, 17, tzinfo=UTC), datetime(2026, 6, 17, 23, tzinfo=UTC),
                   base_cap=Decimal("1000"), candles=_candles(flat, flat, tz=UTC))
    assert rep.n_trades == 1
    assert rep.metrics["closed"] == 1   # TIME exit counts as a real (closed) trade

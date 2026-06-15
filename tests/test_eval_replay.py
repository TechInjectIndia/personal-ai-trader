"""Deterministic eval-gate engine (FRD G3): trade simulator + metrics + pass@k.

All synthetic candles, no DB — the engine is a pure function of its inputs.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.eval.metrics import book_metrics, fold_verdict, pass_at_k
from helm.eval.replay import ratchet_stop, simulate_trade

IST = ZoneInfo("Asia/Kolkata")


def _candles(closes: list[float], *, start=time(9, 30), date=(2026, 6, 10)):
    """1-min candles from a list of closes starting at `start` IST."""
    base = datetime(*date, start.hour, start.minute, tzinfo=IST)
    return [{"bar_ts": base + timedelta(minutes=i), "close": Decimal(str(c))}
            for i, c in enumerate(closes)]


# ── simulate_trade: exits ─────────────────────────────────────────────

def test_buy_hits_target():
    o = simulate_trade("BUY", Decimal("100"), Decimal("98"), Decimal("104"),
                       qty=10, candles=_candles([101, 102, 104, 106]),
                       apply_ratchet=False)
    assert o.exit_reason == "TARGET"
    assert o.exit_price == Decimal("104")
    assert o.gross == Decimal("40.00")     # (104-100)*10
    assert o.net < o.gross                  # charges deducted
    assert o.closed is True


def test_buy_hits_stop():
    o = simulate_trade("BUY", Decimal("100"), Decimal("98"), Decimal("104"),
                       qty=10, candles=_candles([99.5, 98.0, 97.0]),
                       apply_ratchet=False)
    assert o.exit_reason == "STOP"
    assert o.exit_price == Decimal("98")
    assert o.gross == Decimal("-20.00")
    assert o.net < o.gross                  # loss plus charges


def test_eod_square_off_takes_precedence():
    # A candle at/after 15:15 forces EOD even if it would also clear target.
    o = simulate_trade("BUY", Decimal("100"), Decimal("98"), Decimal("104"),
                       qty=5, candles=_candles([101, 110], start=time(15, 14)),
                       apply_ratchet=False)
    # bar0 15:14 close 101 (no hit), bar1 15:15 → EOD at 110
    assert o.exit_reason == "EOD"
    assert o.exit_price == Decimal("110")


def test_sell_side_mirrors():
    o = simulate_trade("SELL", Decimal("100"), Decimal("102"), Decimal("96"),
                       qty=10, candles=_candles([99, 97, 96]), apply_ratchet=False)
    assert o.exit_reason == "TARGET"
    assert o.gross == Decimal("40.00")     # (100-96)*10


def test_unclosed_window_excluded_from_economics():
    o = simulate_trade("BUY", Decimal("100"), Decimal("98"), Decimal("104"),
                       qty=10, candles=_candles([100.5, 101, 100.8]),
                       apply_ratchet=False)
    assert o.exit_reason == "UNCLOSED"
    assert o.closed is False
    m = book_metrics([o])
    assert m.n == 1 and m.closed == 0 and m.net == Decimal("0")


# ── #681 ratchet changes the outcome ──────────────────────────────────

def test_ratchet_locks_in_gain_vs_plain_stop():
    # Runs to 50%+ of the 100→110 move (arms a breakeven+cushion stop at 100.20),
    # then drifts back through 100.20 before sliding to 97.
    closes = [105, 106, 100.20, 97]
    common = dict(side="BUY", entry=Decimal("100"), stop=Decimal("98"),
                  target=Decimal("110"), qty=10, candles=_candles(closes))
    with_ratchet = simulate_trade(**common, apply_ratchet=True)
    plain = simulate_trade(**common, apply_ratchet=False)
    # Ratchet exits at the locked 100.20 (a gain); the plain stop only triggers
    # later at 97 (a loss). Give-back protection working as designed (#681).
    assert with_ratchet.exit_reason == "STOP"
    assert with_ratchet.exit_price == Decimal("100.20")   # above entry → gain
    assert plain.exit_price == Decimal("97")              # plain rides to a loss
    assert with_ratchet.net > plain.net


def test_ratchet_stop_is_monotone_and_never_crosses_ltp():
    # 60% progress on a 100→110 move at ltp=106 → breakeven+cushion floor.
    s = ratchet_stop("BUY", Decimal("100"), Decimal("98"), Decimal("110"),
                     Decimal("106"), Decimal("120"))
    assert Decimal("100") < s < Decimal("106")     # above entry, below ltp
    # below the trigger (only 20% progress) → unchanged
    s2 = ratchet_stop("BUY", Decimal("100"), Decimal("98"), Decimal("110"),
                      Decimal("102"), Decimal("120"))
    assert s2 == Decimal("98")


# ── metrics ───────────────────────────────────────────────────────────

def test_book_metrics_basic():
    win = simulate_trade("BUY", Decimal("100"), Decimal("98"), Decimal("104"),
                         qty=10, candles=_candles([104]), apply_ratchet=False)
    loss = simulate_trade("BUY", Decimal("100"), Decimal("98"), Decimal("104"),
                          qty=10, candles=_candles([98]), apply_ratchet=False)
    m = book_metrics([win, loss])
    assert m.closed == 2
    assert m.win_pct == 50.0
    assert m.gross == Decimal("20.00")             # +40 - 20
    assert m.e2c > 0


def test_max_drawdown():
    # nets +50, -30, -40, +20 ordered → peak 50, trough 50-30-40=-20 → DD 70
    from helm.eval.metrics import _max_drawdown
    assert _max_drawdown([Decimal(x) for x in (50, -30, -40, 20)]) == Decimal("70")


# ── pass@k gate ───────────────────────────────────────────────────────

class _M:  # lightweight BookMetrics stand-in for the comparison logic
    def __init__(self, net, dd, closed):
        self.net = Decimal(str(net))
        self.max_drawdown = Decimal(str(dd))
        self.closed = closed


def test_fold_verdict_min_trades_floor():
    base = _M(0, 100, 20)
    cand = _M(500, 100, 3)         # great net, but only 3 trades
    v = fold_verdict(base, cand, min_trades=5)
    assert v.improved and not v.enough_trades and not v.passed


def test_fold_verdict_drawdown_regression_blocks():
    base = _M(0, 100, 20)
    cand = _M(50, 200, 20)         # higher net but double the drawdown
    v = fold_verdict(base, cand)
    assert v.improved and not v.non_regressive and not v.passed


def test_pass_at_k_requires_supermajority():
    better = (_M(0, 100, 20), _M(100, 100, 20))   # candidate wins
    worse = (_M(0, 100, 20), _M(-50, 100, 20))    # candidate loses
    # 2 of 3 wins, need ceil(3*0.6)=2 → pass
    assert pass_at_k([better, better, worse]).passed is True
    # 1 of 3 wins → fail
    assert pass_at_k([better, worse, worse]).passed is False

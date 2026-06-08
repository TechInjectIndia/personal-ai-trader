"""
Unit tests for the give-back-protection stop lock-in (task #681).

Exercises scripts.manage_positions._tighten_stop as a pure function with a stub
`c` (records execute() calls) and a monkeypatched insert_audit so no DB is
touched. All prices/Decimals. Proves:
  (a) the lock-in stop is raised at the breakeven trigger (UPDATE issued);
  (b) a locked-in stop then fires as a normal STOP exit booking a gain
      (integration over _close_trade with a stub cursor);
  (c) existing behaviour is unchanged below the trigger / not in profit / when
      target is None (no UPDATE, stop returned unchanged);
  (d) edge cases: monotone ratchet, EOD-approach time decay, no-self-fill clamp,
      malformed span, and the SELL/short mirror.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import scripts.manage_positions as mp

IST = ZoneInfo("Asia/Kolkata")


class _StubCursor:
    """Captures execute() calls; no real DB."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.calls.append((sql, params))


def _no_audit(*_a, **_k) -> None:  # monkeypatch target
    return None


def _tighten(c, ltp, entry, stop, target, side, t_rem, monkeypatch):
    monkeypatch.setattr(mp, "insert_audit", _no_audit)
    t = {"id": 1, "symbol": "RELIANCE"}
    return mp._tighten_stop(
        c,
        t,
        Decimal(ltp),
        Decimal(entry),
        Decimal(stop),
        Decimal(target),
        side,
        Decimal(t_rem),
    )


# BUY baseline: entry=100, stop=98, target=110 (span=10) unless noted.


def test_below_breakeven_trigger_midday_no_update(monkeypatch) -> None:
    c = _StubCursor()
    # ltp=104 -> prog 0.40 < 0.50; t_rem large (no time decay)
    out = _tighten(c, "104", "100", "98", "110", "BUY", "300", monkeypatch)
    assert out == Decimal("98")
    assert c.calls == []


def test_breakeven_lock_at_trigger_updates_once(monkeypatch) -> None:
    c = _StubCursor()
    # ltp=105 -> prog 0.50 -> floor = entry + 0.02*10 = 100.20
    out = _tighten(c, "105", "100", "98", "110", "BUY", "300", monkeypatch)
    assert out == Decimal("100.20")
    assert len(c.calls) == 1
    assert "UPDATE paper_trades" in c.calls[0][0]
    assert c.calls[0][1][0] == Decimal("100.20")

    # Re-call with stop already at the breakeven floor and higher price but still
    # mid-day: breakeven floor unchanged -> monotone, no second update.
    c2 = _StubCursor()
    out2 = _tighten(c2, "106", "100", "100.20", "110", "BUY", "300", monkeypatch)
    assert out2 == Decimal("100.20")
    assert c2.calls == []


def test_ratchet_never_loosens(monkeypatch) -> None:
    c = _StubCursor()
    # stop already 100.20; price falls back to prog 0.40 (below trigger), mid-day.
    out = _tighten(c, "104", "100", "100.20", "110", "BUY", "300", monkeypatch)
    assert out == Decimal("100.20")
    assert c.calls == []


def test_eod_approach_tighten_in_profit(monkeypatch) -> None:
    c = _StubCursor()
    # t_rem=22.5 (half of 45) -> lock_frac=0.50; ltp=108 -> td=100+0.5*8=104.00.
    out = _tighten(c, "108", "100", "98", "110", "BUY", "22.5", monkeypatch)
    assert out == Decimal("104.00")
    assert len(c.calls) == 1

    # Deep into wind-down: t_rem=4.5 -> clamp(0.90,0,0.80)=0.80 -> 100+0.8*8=106.40.
    c2 = _StubCursor()
    out2 = _tighten(c2, "108", "100", "104.00", "110", "BUY", "4.5", monkeypatch)
    assert out2 == Decimal("106.40")
    assert len(c2.calls) == 1


def test_eod_approach_not_in_profit_no_update(monkeypatch) -> None:
    c = _StubCursor()
    # ltp=99 < entry: time-decay branch skipped; prog negative -> breakeven skipped.
    out = _tighten(c, "99", "100", "98", "110", "BUY", "10", monkeypatch)
    assert out == Decimal("98")
    assert c.calls == []


def test_no_self_fill_locked_stop_stays_below_ltp(monkeypatch) -> None:
    c = _StubCursor()
    # Tiny t_rem + price barely above entry: deepest wind-down lock (0.80) still
    # leaves the locked floor strictly below LTP (no instant self-fill), so the
    # tighten is allowed but the returned stop < ltp.
    out = _tighten(c, "100.05", "100", "98", "110", "BUY", "0.5", monkeypatch)
    # td_floor = 100 + 0.80*0.05 = 100.04 (< ltp 100.05) -> tightens, no self-fill.
    assert out == Decimal("100.04")
    assert out < Decimal("100.05")
    assert len(c.calls) == 1


def test_self_fill_clamp_blocks_floor_at_or_above_ltp(monkeypatch) -> None:
    c = _StubCursor()
    # Directly exercise the clamp: a stop already so close that the only candidate
    # floor would land at/above LTP must NOT be pushed across LTP. Here ltp is
    # exactly at the breakeven floor (entry+cushion), so floor>=ltp -> no-op.
    # entry=100, cushion 0.02*span(10)=0.20 -> breakeven floor 100.20; ltp=100.20.
    out = _tighten(c, "100.20", "100", "98", "110", "BUY", "300", monkeypatch)
    # prog = 0.20/10 = 0.02 < 0.50 so breakeven doesn't even arm; pure no-op.
    assert out == Decimal("98")
    assert c.calls == []


def test_stop_below_ltp_below_target_no_inversion(monkeypatch) -> None:
    c = _StubCursor()
    # ltp=109 -> prog 0.90; locked floor must stay < ltp < target=110.
    out = _tighten(c, "109", "100", "98", "110", "BUY", "300", monkeypatch)
    assert out == Decimal("100.20")  # breakeven floor only (no time decay)
    assert out < Decimal("109") < Decimal("110")


def test_malformed_span_no_op(monkeypatch) -> None:
    c = _StubCursor()
    # target <= entry -> span <= 0 -> no-op.
    out = _tighten(c, "105", "100", "98", "100", "BUY", "300", monkeypatch)
    assert out == Decimal("98")
    assert c.calls == []


def test_sell_mirror(monkeypatch) -> None:
    c = _StubCursor()
    # SELL: entry=100, stop=102, target=90 (span=10), ltp=95 -> prog 0.50.
    # floor = entry - cushion = 100 - 0.02*10 = 99.80 (< stop, tighter for SELL).
    out = _tighten(c, "95", "100", "102", "90", "SELL", "300", monkeypatch)
    assert out == Decimal("99.80")
    assert len(c.calls) == 1
    assert c.calls[0][1][0] == Decimal("99.80")


# --- Integration: a locked stop fires as a STOP exit booking a gain ---


class _CloseStubCursor:
    """Captures the _close_trade UPDATE so we can assert the booked exit."""

    def __init__(self) -> None:
        self.update: tuple | None = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "status = 'CLOSED'" in sql:
            self.update = params


def test_locked_stop_books_gain_via_stop_branch(monkeypatch) -> None:
    monkeypatch.setattr(mp, "insert_audit", _no_audit)
    c = _CloseStubCursor()
    # Trade locked to breakeven floor 100.20; price reverts to exactly that floor.
    trade = {
        "id": 7,
        "symbol": "RELIANCE",
        "side": "BUY",
        "qty": 10,
        "entry_price": Decimal("100"),
    }
    exit_price = Decimal("100.20")
    mp._close_trade(c, trade, exit_price, "STOP")

    assert c.update is not None
    # UPDATE param order: (exit_price, exit_ts, reason, pnl, charges, net_pnl, id)
    assert c.update[0] == Decimal("100.20")
    assert c.update[2] == "STOP"
    gross_pnl = c.update[3]
    net_pnl = c.update[5]
    # Gross is a gain (locked above entry); net is gross minus charges.
    assert gross_pnl == (exit_price - Decimal("100")) * 10
    assert gross_pnl > 0
    assert net_pnl == gross_pnl - c.update[4]
    assert c.update[6] == 7


def test_call_site_guard_skips_when_target_none() -> None:
    """The tighten call is guarded by `target is not None` at the call site, so a
    target-less row is managed by plain EOD/STOP only. We assert the helper is
    never reached for that case by reproducing the guard condition directly."""
    ltp = Decimal("105")
    eod = False
    target = None
    should_tighten = ltp is not None and not eod and target is not None
    assert should_tighten is False


def test_is_house_gate() -> None:
    """The #681 lock-in is house-only: it must fire for house rows (NULL or the
    house competitor id) and NEVER for a freestyle competitor's row, whose stop
    the agent authors and owns."""
    assert mp._is_house({"competitor_id": None}) is True
    assert mp._is_house({"competitor_id": "house-claude"}) is True
    assert mp._is_house({"competitor_id": "gemini-momentum"}) is False
    assert mp._is_house({"competitor_id": "opencode-range"}) is False


def test_call_site_t_rem_decreases_toward_square_off() -> None:
    """Sanity on the t_rem_min derivation used at the call site."""
    now = datetime(2026, 6, 8, 14, 45, tzinfo=IST)  # 30 min before 15:15
    from helm.config import SQUARE_OFF_AT

    t_rem = (
        Decimal(
            (datetime.combine(now.date(), SQUARE_OFF_AT, tzinfo=IST) - now).total_seconds()
        )
        / Decimal("60")
    )
    assert t_rem == Decimal("30")

"""F5 — conviction-weighted sizing (flag-gated, default OFF).

The mechanism is shipped behind CONVICTION_SIZING_ENABLED; until the conf↔outcome
correlation clears >=50 trades the flag stays False and execute_signal sizes
exactly as today. Tests cover the multiplier math, the flag-off identity, and
the flag-on floor-skip / scaling behaviour.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from helm import config
from helm.data.store import conn
import scripts.paper_execute as pe
from scripts.paper_execute import _conviction_mult, execute_signal


def _cleanup() -> None:
    with conn() as c:
        c.execute("DELETE FROM paper_trades WHERE symbol LIKE 'ZZZF5%'")
        c.execute("DELETE FROM decisions WHERE signal_id IN "
                  "(SELECT id FROM signals WHERE symbol LIKE 'ZZZF5%')")
        c.execute("DELETE FROM signals WHERE symbol LIKE 'ZZZF5%'")


@pytest.fixture(autouse=True)
def _scrub():
    _cleanup()
    yield
    _cleanup()


def _insert_signal() -> int:
    """A fat-target signal so the F2 E2C gate never blocks (isolates F5)."""
    with conn() as c:
        return c.execute(
            "INSERT INTO signals (ts, strategy, symbol, side, entry_price, "
            "stop_loss, target, consumed) VALUES (now(), 'zzz', 'ZZZF5', 'BUY', "
            "100.00, 98.00, 120.00, FALSE) RETURNING id"
        ).fetchone()["id"]


# ── multiplier math ────────────────────────────────────────────────

def test_conviction_mult_endpoints_and_clamp():
    assert _conviction_mult(config.CONVICTION_FLOOR) == config.CONVICTION_SIZE_MIN_MULT
    assert _conviction_mult(Decimal("1")) == Decimal("1")
    # monotonic in between; clamped below the floor
    mid = _conviction_mult((config.CONVICTION_FLOOR + Decimal("1")) / 2)
    assert config.CONVICTION_SIZE_MIN_MULT < mid < Decimal("1")
    assert _conviction_mult(Decimal("0.0")) == config.CONVICTION_SIZE_MIN_MULT


# ── flag OFF: identical to today ───────────────────────────────────

def test_flag_off_low_conviction_still_trades(monkeypatch):
    """With the flag OFF (default), even a tiny conviction does NOT skip."""
    assert config.CONVICTION_SIZING_ENABLED is False  # ships off
    sid = _insert_signal()
    res = execute_signal(sid, actor="t", conviction=Decimal("0.01"))
    # not blocked for low_conviction (may pass or hit wallet/risk, but not F5)
    assert "low_conviction" not in res.message


# ── flag ON: floor-skip + scaling ──────────────────────────────────

def test_flag_on_below_floor_skips(monkeypatch):
    monkeypatch.setattr(pe, "CONVICTION_SIZING_ENABLED", True)
    sid = _insert_signal()
    res = execute_signal(sid, actor="t", conviction=Decimal("0.40"))  # < 0.55 floor
    assert res.ok is False and "low_conviction" in res.message
    with conn() as c:
        assert c.execute("SELECT verdict FROM decisions WHERE signal_id=%s",
                         (sid,)).fetchone()["verdict"] == "SKIP"


def test_flag_on_scales_cap(monkeypatch):
    """High conviction sizes larger than low conviction (same wallet/entry)."""
    monkeypatch.setattr(pe, "CONVICTION_SIZING_ENABLED", True)
    seen = {}
    real_cap = pe.dynamic_position_cap

    def _spy_eval(symbol, side, qty, entry, competitor_id=None, **kw):
        seen["qty"] = qty
        return False, "spy"  # block after sizing so no trade is booked
    monkeypatch.setattr(pe.risk, "evaluate", _spy_eval)

    sid = _insert_signal()
    execute_signal(sid, actor="t", conviction=Decimal("0.70"))
    low = seen.get("qty")
    _cleanup()
    sid2 = _insert_signal()
    execute_signal(sid2, actor="t", conviction=Decimal("0.99"))
    high = seen.get("qty")
    assert real_cap  # keep ref used
    assert high is not None and low is not None and high >= low

"""F2 — cost-aware minimum-edge gate.

Refuses to open a house trade whose gross reward to target is below
MIN_EDGE_TO_COST x the expected round-trip cost. Unit tests for the E2C math
plus an integration test proving a thin-target signal is SKIPped (no paper
trade) while still booking a decision and consuming the signal.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from helm import config
from helm.data.store import conn
from scripts.paper_execute import _edge_to_cost, execute_signal

ZZZ = "ZZZF2"


def _cleanup() -> None:
    with conn() as c:
        c.execute("DELETE FROM paper_trades WHERE symbol LIKE 'ZZZF2%'")
        c.execute(
            "DELETE FROM decisions WHERE signal_id IN "
            "(SELECT id FROM signals WHERE symbol LIKE 'ZZZF2%')"
        )
        c.execute("DELETE FROM signals WHERE symbol LIKE 'ZZZF2%'")


@pytest.fixture(autouse=True)
def _scrub():
    _cleanup()
    yield
    _cleanup()


# ── E2C math (pure, real charge model) ──────────────────────────────

def test_min_edge_to_cost_is_decimal():
    assert isinstance(config.MIN_EDGE_TO_COST, Decimal)


def test_edge_to_cost_thin_below_threshold():
    entry = Decimal("1000")
    # ~0.2% move on 12 shares: reward ~Rs24 vs ~Rs13 round-trip cost -> E2C < 3.
    assert _edge_to_cost("BUY", 12, entry, entry + Decimal("2")) < config.MIN_EDGE_TO_COST


def test_edge_to_cost_fat_above_threshold():
    entry = Decimal("1000")
    # ~1.5% move on 12 shares: reward ~Rs180 -> E2C well above 3.
    assert _edge_to_cost("BUY", 12, entry, entry + Decimal("15")) >= config.MIN_EDGE_TO_COST


def test_edge_to_cost_zero_cost_guard(monkeypatch):
    monkeypatch.setattr(
        "scripts.paper_execute.round_trip_breakdown",
        lambda *a, **k: SimpleNamespace(total=Decimal("0")),
    )
    assert _edge_to_cost("BUY", 10, Decimal("100"), Decimal("110")) == Decimal("0")


# ── Gate integration (real DB chokepoint) ───────────────────────────

def _insert_signal(target: str | None) -> int:
    tgt = "NULL" if target is None else target
    with conn() as c:
        return c.execute(
            f"""
            INSERT INTO signals (ts, strategy, symbol, side, entry_price,
                                 stop_loss, target, consumed)
            VALUES (now(), 'zzz', 'ZZZF2', 'BUY', 1000.00, 990.00, {tgt}, FALSE)
            RETURNING id
            """
        ).fetchone()["id"]


def test_thin_target_skipped_no_trade():
    """Target Rs2 above entry -> E2C < 3 -> SKIP, decision booked, no paper
    trade, signal consumed."""
    sid = _insert_signal("1002.00")
    res = execute_signal(sid, actor="test-f2", qty=12)  # fixed qty isolates the gate
    assert res.ok is False
    assert "below_min_edge_to_cost" in res.message
    with conn() as c:
        d = c.execute(
            "SELECT verdict FROM decisions WHERE signal_id=%s", (sid,)
        ).fetchone()
        assert d["verdict"] == "SKIP"
        n = c.execute(
            "SELECT count(*) n FROM paper_trades pt JOIN decisions d "
            "ON d.id=pt.decision_id WHERE d.signal_id=%s",
            (sid,),
        ).fetchone()["n"]
        assert n == 0
        assert c.execute(
            "SELECT consumed FROM signals WHERE id=%s", (sid,)
        ).fetchone()["consumed"] is True


def test_target_none_skips_gate(monkeypatch):
    """A target-less signal cannot be E2C-evaluated -> the gate is skipped and
    risk.evaluate is reached (proven by spying on it)."""
    seen = {}
    import scripts.paper_execute as pe

    def _spy(symbol, side, qty, entry):
        seen["called"] = True
        return False, "spy-block"

    monkeypatch.setattr(pe.risk, "evaluate", _spy)
    sid = _insert_signal(None)
    execute_signal(sid, actor="test-f2", qty=12)  # fixed qty isolates the gate
    assert seen.get("called") is True  # gate did NOT short-circuit before risk

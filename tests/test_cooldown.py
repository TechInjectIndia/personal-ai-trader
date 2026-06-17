"""Per-symbol re-entry cooldown gate in risk.evaluate (wired 2026-06-08).

Previously `per_symbol_cooldown_min` was defined but never enforced. These
tests cover the new `minutes_since_last_exit` helper (DB integration) and the
cooldown branch in `evaluate` (logic, via monkeypatch of the upstream gates).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from helm.data.store import conn
from helm.orchestrator import risk

ZZZ = "ZZZCD"


def _cleanup() -> None:
    with conn() as c:
        c.execute("DELETE FROM paper_trades WHERE symbol = %s", (ZZZ,))


@pytest.fixture(autouse=True)
def _scrub():
    _cleanup()
    yield
    _cleanup()


def _insert_closed(mins_ago: int) -> None:
    """A house (competitor_id NULL) CLOSED trade in ZZZ that exited mins_ago."""
    with conn() as c:
        c.execute(
            """
            INSERT INTO paper_trades
                (symbol, side, qty, entry_price, stop_loss, status,
                 entry_ts, exit_ts, exit_price, pnl_inr, net_pnl_inr)
            VALUES (%s, 'BUY', 1, 100.00, 99.00, 'CLOSED',
                    now() - (%s||' minutes')::interval - interval '1 minute',
                    now() - (%s||' minutes')::interval, 101.00, 1.00, 0.50)
            """,
            (ZZZ, mins_ago, mins_ago),
        )


# ── minutes_since_last_exit (DB integration) ───────────────────────

def test_minutes_since_last_exit_none_when_no_trade():
    assert risk.minutes_since_last_exit(ZZZ) is None


def test_minutes_since_last_exit_recent():
    _insert_closed(10)
    mins = risk.minutes_since_last_exit(ZZZ)
    assert mins is not None
    assert Decimal("9") <= mins <= Decimal("12")  # ~10, allow clock slack


# ── cooldown branch in evaluate (logic) ────────────────────────────

def _pass_upstream(monkeypatch):
    """Make every gate BEFORE the cooldown check pass, so we isolate it."""
    monkeypatch.setattr(risk, "kill_engaged_today", lambda *a, **k: False)
    monkeypatch.setattr(risk, "todays_realized_pnl", lambda *a, **k: Decimal("0"))
    monkeypatch.setattr(risk, "open_paper_positions", lambda *a, **k: 0)
    monkeypatch.setattr(risk, "has_open_position", lambda *a, **k: False)
    monkeypatch.setattr(risk, "signals_for_symbol_today", lambda *a, **k: 0)


def test_cooldown_blocks_within_window(monkeypatch):
    _pass_upstream(monkeypatch)
    monkeypatch.setattr(risk, "minutes_since_last_exit", lambda *a, **k: Decimal("10"))
    allowed, reason = risk.evaluate("INFY", "BUY", 1, Decimal("100"))
    assert allowed is False
    assert "cooldown" in reason


def test_cooldown_clears_after_window(monkeypatch):
    _pass_upstream(monkeypatch)
    monkeypatch.setattr(risk, "minutes_since_last_exit", lambda *a, **k: Decimal("60"))
    _, reason = risk.evaluate("INFY", "BUY", 1, Decimal("100"))
    assert "cooldown" not in reason  # passed the cooldown gate (downstream may still deny)


def test_cooldown_skipped_when_no_prior_trade(monkeypatch):
    _pass_upstream(monkeypatch)
    monkeypatch.setattr(risk, "minutes_since_last_exit", lambda *a, **k: None)
    _, reason = risk.evaluate("INFY", "BUY", 1, Decimal("100"))
    assert "cooldown" not in reason

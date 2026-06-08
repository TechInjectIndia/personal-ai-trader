"""F6 A/B unblock — per-(symbol,strategy) position slots (flag-gated, default OFF).

has_open_position can optionally key on strategy so distinct house strategies
hold concurrent positions in the same symbol. evaluate only passes a strategy
key when HOUSE_STRATEGY_KEYED_SLOTS is on AND it's the house path.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from helm.data.store import conn
from helm.orchestrator import risk

ZZZ = "ZZZSLOT"


def _cleanup() -> None:
    with conn() as c:
        c.execute("DELETE FROM paper_trades WHERE symbol LIKE 'ZZZSLOT%'")
        c.execute("DELETE FROM decisions WHERE signal_id IN "
                  "(SELECT id FROM signals WHERE symbol LIKE 'ZZZSLOT%')")
        c.execute("DELETE FROM signals WHERE symbol LIKE 'ZZZSLOT%'")


@pytest.fixture(autouse=True)
def _scrub():
    _cleanup()
    yield
    _cleanup()


def _open_trade(strategy: str) -> None:
    """An OPEN house trade in ZZZ via the signals→decisions→paper_trades chain."""
    with conn() as c:
        sid = c.execute(
            "INSERT INTO signals (ts, strategy, symbol, side, entry_price, "
            "stop_loss, target, consumed) VALUES (now(), %s, %s, 'BUY', 100, 99, "
            "120, TRUE) RETURNING id", (strategy, ZZZ)).fetchone()["id"]
        did = c.execute(
            "INSERT INTO decisions (signal_id, actor, verdict, qty, final_entry, "
            "final_stop, final_target) VALUES (%s,'t','TAKE',1,100,99,120) RETURNING id",
            (sid,)).fetchone()["id"]
        c.execute(
            "INSERT INTO paper_trades (decision_id, symbol, side, qty, entry_price, "
            "entry_ts, stop_loss, target, status) VALUES "
            "(%s, %s, 'BUY', 1, 100, now(), 99, 120, 'OPEN')", (did, ZZZ))


def test_symbol_keyed_blocks_any_strategy():
    """Default (no strategy key): an open position in the symbol blocks all."""
    _open_trade("bbands_zscore_20")
    assert risk.has_open_position(ZZZ) is True
    assert risk.has_open_position(ZZZ, strategy="bbands_zscore_20_5m") is False  # other strat free
    assert risk.has_open_position(ZZZ, strategy="bbands_zscore_20") is True      # same strat busy


def test_evaluate_flag_off_is_symbol_keyed(monkeypatch):
    """Flag OFF (default): a different strategy is still blocked by the open slot."""
    monkeypatch.setattr(risk, "HOUSE_STRATEGY_KEYED_SLOTS", False)
    monkeypatch.setattr(risk, "kill_engaged_today", lambda c=None: False)
    monkeypatch.setattr(risk, "todays_realized_pnl", lambda c=None: Decimal("0"))
    monkeypatch.setattr(risk, "open_paper_positions", lambda c=None: 0)
    _open_trade("bbands_zscore_20")
    allowed, reason = risk.evaluate(ZZZ, "BUY", 1, Decimal("100"),
                                    strategy="bbands_zscore_20_5m")
    assert allowed is False and "open position" in reason


def test_evaluate_flag_on_allows_other_strategy(monkeypatch):
    """Flag ON: a different house strategy may open concurrently in the symbol."""
    monkeypatch.setattr(risk, "HOUSE_STRATEGY_KEYED_SLOTS", True)
    monkeypatch.setattr(risk, "kill_engaged_today", lambda c=None: False)
    monkeypatch.setattr(risk, "todays_realized_pnl", lambda c=None: Decimal("0"))
    monkeypatch.setattr(risk, "open_paper_positions", lambda c=None: 0)
    monkeypatch.setattr(risk, "signals_for_symbol_today", lambda s, c=None: 0)
    monkeypatch.setattr(risk, "minutes_since_last_exit", lambda s, c=None: None)
    _open_trade("bbands_zscore_20")
    # different strategy → cleared past the open-position gate (downstream wallet
    # may still deny, but NOT for "open position")
    _, reason = risk.evaluate(ZZZ, "BUY", 1, Decimal("100"),
                              strategy="bbands_zscore_20_5m")
    assert "open position" not in reason
    # same strategy → still blocked
    _, reason2 = risk.evaluate(ZZZ, "BUY", 1, Decimal("100"),
                               strategy="bbands_zscore_20")
    assert "open position" in reason2

"""M6 — crypto enablement: session-strategy allowlist, registry shape, and the
24/7 time-stop in manage_positions (integration, mm_test only)."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from helm.markets.registry import MARKET_CRYPTO, MARKET_IN
from helm.strategies.intraday.mean_reversion import MeanReversion
from helm.strategies.intraday.orb import OpeningRangeBreakout
from scripts.scan_signals import _strategy_allowed


def test_session_strategies_blocked_on_crypto_allowed_on_in():
    orb = OpeningRangeBreakout(or_minutes=15)
    bbands = MeanReversion()
    assert orb.session_required is True
    assert _strategy_allowed(orb, MARKET_IN) is True          # sessioned venue: ok
    assert _strategy_allowed(orb, MARKET_CRYPTO) is False      # 24/7: no opening range
    assert _strategy_allowed(bbands, MARKET_CRYPTO) is True    # session-agnostic: ok
    assert _strategy_allowed(bbands, MARKET_IN) is True


def test_crypto_registry_shape():
    assert MARKET_CRYPTO.key == "CRYPTO"
    assert MARKET_CRYPTO.currency == "USD"
    assert MARKET_CRYPTO.fractional is True
    assert MARKET_CRYPTO.max_hold_min == 240
    assert MARKET_CRYPTO.calendar.square_off_at() is None       # 24/7, no flatten


@pytest.mark.skipif(os.environ.get("HELM_SEARCH_PATH") != "mm_test",
                    reason="integration test; needs the isolated mm_test schema")
def test_manage_positions_time_stop_closes_crypto(monkeypatch):
    from helm.data.store import conn
    import scripts.manage_positions as mp

    with conn() as c:
        for t in ("paper_trades", "decisions"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608
        d = c.execute("INSERT INTO decisions (actor,verdict,qty) VALUES ('t','TAKE',0.1) "
                      "RETURNING id").fetchone()["id"]
        # Open BTC trade entered 300 min ago (> 240 max_hold); far stop/target so
        # only the time-stop can close it.
        c.execute(
            "INSERT INTO paper_trades (decision_id,symbol,market,side,qty,entry_price,"
            "entry_ts,stop_loss,target,status) VALUES (%s,'BTC','CRYPTO','BUY',0.1,100,"
            "now() - interval '300 minutes',50,200,'OPEN')",
            (d,),
        )
    # Avoid the network: crypto LTP is stubbed (no stop/target hit at 100).
    monkeypatch.setattr(MARKET_CRYPTO.data, "last_price", lambda symbol: Decimal("100"))

    mp.main()

    with conn() as c:
        row = c.execute("SELECT status, exit_reason FROM paper_trades "
                        "WHERE market = 'CRYPTO'").fetchone()
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "TIME"

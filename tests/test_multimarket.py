"""M2 — schema namespacing, per-market wallets, fractional qty.

INTEGRATION test: runs only against the isolated `mm_test` schema (set
HELM_SEARCH_PATH=mm_test), so it never touches the live `public` data and is
skipped in a normal CI run.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HELM_SEARCH_PATH") != "mm_test",
    reason="integration test; needs the isolated mm_test schema",
)

from helm.data.store import conn  # noqa: E402
from helm.markets import MARKET_IN  # noqa: E402
from helm.markets.base import Market  # noqa: E402
from helm.markets.registry import _register  # noqa: E402
from helm.wallet import wallet_state  # noqa: E402
from scripts.paper_execute import execute_signal  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _wipe() -> None:
    with conn() as c:
        for t in ("paper_trades", "decisions", "signals", "candles_1m", "wallets"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608 — fixed table names


def _seed_signal(symbol: str, market: str, entry: Decimal, stop: Decimal,
                 target: Decimal, fill_open: Decimal) -> int:
    """Insert a signal + the next-minute candle used for the realistic fill."""
    now = datetime.now(IST).replace(second=0, microsecond=0)
    fill_bar = now + timedelta(minutes=1)
    with conn() as c:
        c.execute(
            "INSERT INTO candles_1m (market, symbol, bar_ts, open, high, low, close, tick_count) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,1)",
            (market, symbol, fill_bar, fill_open, fill_open, fill_open, fill_open),
        )
        row = c.execute(
            "INSERT INTO signals (ts, strategy, symbol, market, side, entry_price, "
            "stop_loss, target, rationale, payload) "
            "VALUES (%s,'t',%s,%s,'BUY',%s,%s,%s,'r','{}'::jsonb) RETURNING id",
            (now, symbol, market, entry, stop, target),
        ).fetchone()
    return row["id"]


def test_in_round_trip_stamps_market_and_keeps_integer_qty():
    _wipe()
    sid = _seed_signal("RELIANCE", "IN", Decimal("100"), Decimal("99"),
                       Decimal("103"), Decimal("100"))
    res = execute_signal(sid, actor="test")
    assert res.ok, res.message
    with conn() as c:
        row = c.execute(
            "SELECT market, qty FROM paper_trades WHERE decision_id = %s", (res.decision_id,)
        ).fetchone()
    assert row["market"] == "IN"
    # IN is integer-lots: floor(15000 / 100) = 150, stored as a whole number.
    assert row["qty"] == Decimal("150")


def test_wallets_do_not_co_mingle_across_markets():
    _wipe()
    with conn() as c:
        c.execute("INSERT INTO wallets (market,currency,initial_capital,goal_capital) "
                  "VALUES ('US','USD',1000,2000)")
        # A CLOSED IN trade (+₹500 net) and a CLOSED US trade (+$900 net).
        for mkt, net in (("IN", 500), ("US", 900)):
            d = c.execute("INSERT INTO decisions (actor,verdict,qty) VALUES ('t','TAKE',1) "
                          "RETURNING id").fetchone()["id"]
            c.execute(
                "INSERT INTO paper_trades (decision_id,symbol,market,side,qty,entry_price,"
                "entry_ts,stop_loss,target,exit_price,exit_ts,exit_reason,pnl_inr,"
                "net_pnl_inr,status) VALUES (%s,'X',%s,'BUY',1,100,now(),99,103,101,now(),"
                "'TARGET',%s,%s,'CLOSED')",
                (d, mkt, net, net),
            )
    assert wallet_state("IN").realised_net_pnl == Decimal("500")
    assert wallet_state("US").realised_net_pnl == Decimal("900")
    # IN wallet is INR-config (50000); US wallet reads the USD wallets row.
    assert wallet_state("IN").initial == Decimal("50000")
    assert wallet_state("US").initial == Decimal("1000")


def test_fractional_market_sizes_below_one_unit():
    _wipe()
    # Register a throwaway fractional market (reuses IN's adapters — unused here).
    _register(Market(key="XF", name="frac", data=MARKET_IN.data,
                      calendar=MARKET_IN.calendar, costs=MARKET_IN.costs,
                      currency="USD", fractional=True, watchlist=("FOO",)))
    with conn() as c:
        c.execute("INSERT INTO wallets (market,currency,initial_capital,goal_capital) "
                  "VALUES ('XF','USD',1000,2000)")
    sid = _seed_signal("FOO", "XF", Decimal("200"), Decimal("196"),
                       Decimal("210"), Decimal("200"))
    res = execute_signal(sid, actor="test")
    assert res.ok, res.message
    with conn() as c:
        row = c.execute(
            "SELECT market, qty FROM paper_trades WHERE decision_id = %s", (res.decision_id,)
        ).fetchone()
    assert row["market"] == "XF"
    # 1000 USD / 200 = 5.0 units — fractional path stores a NUMERIC, not floored.
    assert row["qty"] == Decimal("5.00000000")

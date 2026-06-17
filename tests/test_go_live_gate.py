"""M8 — per-market go-live funding gate (integration, mm_test only)."""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HELM_SEARCH_PATH") != "mm_test",
    reason="integration test; needs the isolated mm_test schema",
)

from helm.data.store import conn  # noqa: E402
from scripts.go_live_readiness import evaluate_market  # noqa: E402


def _wipe():
    with conn() as c:
        c.execute("DELETE FROM paper_trades WHERE market = 'IN'")
        c.execute("DELETE FROM backtest_runs WHERE market = 'IN'")


def _seed_paper(n: int, net_each: int, gross_each: int = 13, charge_each: int = 3):
    with conn() as c:
        for i in range(n):
            c.execute(
                "INSERT INTO paper_trades (symbol,market,side,qty,entry_price,entry_ts,"
                "stop_loss,target,exit_price,exit_ts,exit_reason,pnl_inr,charges_inr,"
                "net_pnl_inr,status) VALUES ('X','IN','BUY',1,100,"
                "now() - make_interval(days => %s), 99,103,101,"
                "now() - make_interval(days => %s), 'TARGET',%s,%s,%s,'CLOSED')",
                (i % 12, i % 12, gross_each, charge_each, net_each),
            )


def _seed_backtest(net: float):
    with conn() as c:
        c.execute(
            "INSERT INTO backtest_runs (strategy,market,symbol,bar_minutes,start_ts,end_ts,"
            "decider,n_signals,n_trades,metrics) VALUES ('s','IN','X',1,now(),now(),"
            "'take_all',20,20,%s::jsonb)",
            (json.dumps({"net": net, "closed": 20}),),
        )


def test_ready_when_paper_and_backtest_positive():
    _wipe()
    _seed_paper(30, net_each=10)   # +10/trade, 12 distinct days, cost drag 3/13≈0.23
    _seed_backtest(50.0)
    r = evaluate_market("IN")
    assert r["paper_pass"] is True
    assert r["backtest_pass"] is True
    assert r["ready"] is True


def test_not_ready_when_paper_negative():
    _wipe()
    _seed_paper(30, net_each=-5, gross_each=-2)   # losing book
    _seed_backtest(50.0)                          # even with a good backtest
    r = evaluate_market("IN")
    assert r["paper_pass"] is False
    assert r["ready"] is False
    assert any("expectancy" in x for x in r["reasons"])


def test_not_ready_when_backtest_missing():
    _wipe()
    _seed_paper(30, net_each=10)   # paper passes
    # no backtest evidence
    r = evaluate_market("IN")
    assert r["paper_pass"] is True
    assert r["backtest_pass"] is False
    assert r["ready"] is False
    assert any("backtest" in x for x in r["reasons"])


def test_not_ready_insufficient_trades():
    _wipe()
    _seed_paper(5, net_each=10)
    _seed_backtest(50.0)
    r = evaluate_market("IN")
    assert r["ready"] is False
    assert any("closed paper trades" in x for x in r["reasons"])

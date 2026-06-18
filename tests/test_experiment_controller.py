"""#4 — experiment controller state machine (integration, mm_test only)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HELM_SEARCH_PATH") != "mm_test",
                                reason="integration test; needs the isolated mm_test schema")

import scripts.experiment_controller as ec  # noqa: E402
from helm.data.store import conn  # noqa: E402


def _seed_closed(net: int, exit_ago_min: int):
    with conn() as c:
        d = c.execute("INSERT INTO decisions (actor,verdict,qty) VALUES ('t','TAKE',1) "
                      "RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO paper_trades (decision_id,symbol,market,side,qty,entry_price,"
                  "entry_ts,stop_loss,target,exit_price,exit_ts,exit_reason,pnl_inr,net_pnl_inr,"
                  "status) VALUES (%s,'X','IN','BUY',1,100,now(),99,103,101,"
                  "now() - make_interval(mins => %s),'TARGET',%s,%s,'CLOSED')",
                  (d, exit_ago_min, net, net))


def _wipe():
    with conn() as c:
        c.execute("DELETE FROM paper_trades")
        c.execute("DELETE FROM decisions")
        c.execute("DELETE FROM flag_experiments WHERE flag='TEST_FLAG'")


def test_controller_runs_ab_then_locks_winner():
    _wipe()
    with conn() as c:
        c.execute("INSERT INTO flag_experiments (flag,status,phase,phase_started_ts,min_trades,"
                  "max_days) VALUES ('TEST_FLAG','watching','on', now() - interval '2 hours', 2, 99)")
    # ON arm: 3 winners closed 1h ago (after the 2h-ago phase start)
    for _ in range(3):
        _seed_closed(5, 60)
    acts = ec.run_once()
    assert any(a["action"] == "flipped" and a["to"] == "off" for a in acts), acts

    # OFF arm: 2 losers closed just now (after the flip)
    for _ in range(2):
        _seed_closed(-10, 0)
    acts2 = ec.run_once()
    assert any(a["action"] == "decided" and a["verdict"] == "keep_on" for a in acts2), acts2

    with conn() as c:
        row = c.execute("SELECT status, verdict FROM flag_experiments "
                        "WHERE flag='TEST_FLAG'").fetchone()
    assert row["status"] == "locked_on" and row["verdict"] == "keep_on"
    _wipe()

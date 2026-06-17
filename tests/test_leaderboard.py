"""S3 — fund-only-winners leaderboard (integration, mm_test only)."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HELM_SEARCH_PATH") != "mm_test",
                                reason="integration test; needs the isolated mm_test schema")

from helm.data.store import conn  # noqa: E402
from helm.eval.leaderboard import combo_scores, fund_candidates  # noqa: E402


def _seed(market, strategy, n, net_each, competitor=None):
    with conn() as c:
        for _ in range(n):
            sid = c.execute(
                "INSERT INTO signals (ts,strategy,symbol,market,side,entry_price,stop_loss,"
                "target,competitor_id,consumed) VALUES (now(),%s,'X',%s,'BUY',100,99,103,%s,TRUE) "
                "RETURNING id", (strategy, market, competitor)).fetchone()["id"]
            did = c.execute("INSERT INTO decisions (signal_id,actor,verdict,qty,competitor_id) "
                            "VALUES (%s,'t','TAKE',1,%s) RETURNING id",
                            (sid, competitor)).fetchone()["id"]
            c.execute(
                "INSERT INTO paper_trades (decision_id,symbol,market,side,qty,entry_price,entry_ts,"
                "stop_loss,target,exit_price,exit_ts,exit_reason,pnl_inr,charges_inr,net_pnl_inr,"
                "status,competitor_id) VALUES (%s,'X',%s,'BUY',1,100,now(),99,103,101,now(),"
                "'TARGET',%s,1,%s,'CLOSED',%s)",
                (did, market, Decimal(net_each) + 1, net_each, competitor))


def _wipe():
    with conn() as c:
        for t in ("paper_trades", "decisions", "signals"):
            c.execute(f"DELETE FROM {t}")  # noqa: S608


def test_ranks_winners_above_losers_and_flags_candidates():
    _wipe()
    _seed("IN", "winner", 30, 10)     # +10/trade
    _seed("IN", "loser", 30, -5)      # -5/trade
    _seed("CRYPTO", "thin", 5, 8)     # winning but too few trades

    scores = combo_scores(days=3650, min_trades=1)
    labels = [s.label() for s in scores]
    # winner ranks above loser (expectancy desc)
    assert labels.index("IN:house:winner") < labels.index("IN:house:loser")
    winner = next(s for s in scores if s.strategy == "winner")
    assert winner.expectancy == Decimal("10.00")
    assert winner.win_pct == Decimal("100.00")

    cands = {c.label() for c in fund_candidates(days=3650, min_trades=30)}
    assert "IN:house:winner" in cands       # positive expectancy + enough trades
    assert "IN:house:loser" not in cands    # negative expectancy
    assert "CRYPTO:house:thin" not in cands  # too few trades


def test_separates_by_market_and_competitor():
    _wipe()
    _seed("IN", "s", 10, 5, competitor=None)            # house
    _seed("IN", "s", 10, 7, competitor="gemini-flash")  # a competitor, same strat
    scores = {(s.market, s.competitor_id, s.strategy): s for s in combo_scores(days=3650)}
    assert (("IN", None, "s") in scores) or (("IN", "house-claude", "s") in scores)
    assert ("IN", "gemini-flash", "s") in scores       # distinct combo, not merged

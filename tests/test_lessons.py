"""S5 — per-market lessons in the instinct ledger (integration, mm_test only)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HELM_SEARCH_PATH") != "mm_test",
                                reason="integration test; needs the isolated mm_test schema")

from helm.agents.instincts import lessons_for, promote_cluster_to_instinct  # noqa: E402
from helm.data.store import conn  # noqa: E402


def _wipe():
    with conn() as c:
        c.execute("DELETE FROM agent_instincts WHERE competitor_id = 'house'")
        c.execute("DELETE FROM proposal_clusters WHERE theme LIKE 'S5 %'")


def test_promote_stores_market_and_lessons_for_scopes():
    _wipe()
    with conn() as c:
        cid = c.execute(
            "INSERT INTO proposal_clusters (theme,layer,target_surface,status) "
            "VALUES ('S5 crypto lesson','risk','house','verified') RETURNING id"
        ).fetchone()["id"]
        # a market-agnostic lesson + a US-only lesson, same agent
        for stmt, mkt in (("S5 agnostic", None), ("S5 us only", "US")):
            c.execute("INSERT INTO agent_instincts (competitor_id,statement,confidence,"
                      "status,market,promoted_ts,updated_ts) "
                      "VALUES ('house',%s,1.0,'active',%s,now(),now())", (stmt, mkt))

    r = promote_cluster_to_instinct(cid, market="CRYPTO")
    assert r["market"] == "CRYPTO"

    crypto = {x["statement"] for x in lessons_for("house", "CRYPTO")}
    assert "S5 crypto lesson" in crypto      # market-specific
    assert "S5 agnostic" in crypto           # agnostic applies everywhere
    assert "S5 us only" not in crypto        # other market excluded

    us = {x["statement"] for x in lessons_for("house", "US")}
    assert "S5 us only" in us and "S5 agnostic" in us
    assert "S5 crypto lesson" not in us

    # No market arg → all active lessons for the agent regardless of scope.
    everything = {x["statement"] for x in lessons_for("house")}
    assert {"S5 crypto lesson", "S5 agnostic", "S5 us only"} <= everything
    _wipe()

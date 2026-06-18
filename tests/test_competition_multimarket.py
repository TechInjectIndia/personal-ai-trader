"""Per-(competitor, market) wallet + execution isolation (integration, mm_test)."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HELM_SEARCH_PATH") != "mm_test",
                                reason="integration test; needs the isolated mm_test schema")

from helm.competition import execute as ex  # noqa: E402
from helm.competition import wallet as wl  # noqa: E402
from helm.data.store import conn  # noqa: E402


def _mkcompetitor(cid="t-multi"):
    with conn() as c:
        c.execute("INSERT INTO competitors (id,name,backend,model,persona,autonomy_level,status) "
                  "VALUES (%s,'T','claude','m','p','freestyle','active') "
                  "ON CONFLICT (id) DO NOTHING", (cid,))
        # seed only the IN wallet (US/crypto must auto-seed on first use)
        c.execute("INSERT INTO competitor_wallets (competitor_id,market,currency,"
                  "initial_capital_inr,available_inr,realized_pnl_inr) "
                  "VALUES (%s,'IN','INR',50000,50000,0) ON CONFLICT DO NOTHING", (cid,))
    return cid


def _wipe(cid="t-multi"):
    with conn() as c:
        c.execute("DELETE FROM paper_trades WHERE competitor_id=%s", (cid,))
        c.execute("DELETE FROM decisions WHERE competitor_id=%s", (cid,))
        c.execute("DELETE FROM signals WHERE competitor_id=%s", (cid,))
        c.execute("DELETE FROM competitor_wallets WHERE competitor_id=%s", (cid,))
        c.execute("DELETE FROM competitors WHERE id=%s", (cid,))


def test_us_wallet_autoseeds_and_is_currency_correct():
    cid = _mkcompetitor()
    try:
        st = wl.competitor_wallet_state(cid, "US")     # triggers ensure/seed
        assert st.initial == Decimal("5000")           # US seed (USD), not ₹50k
        with conn() as c:
            row = c.execute("SELECT currency FROM competitor_wallets WHERE competitor_id=%s "
                            "AND market='US'", (cid,)).fetchone()
        assert row["currency"] == "USD"
    finally:
        _wipe()


def test_crypto_open_lands_in_crypto_book_only():
    cid = _mkcompetitor()
    try:
        # Open a fractional crypto position; BTC ~ $100 here for a clean qty.
        res = ex.execute_competitor_open(
            cid, "BTC", "BUY", Decimal("100"), Decimal("95"), Decimal("110"),
            qty=None, actor=cid, rationale="t", market="CRYPTO",
        )
        assert res.ok, res.message
        with conn() as c:
            row = c.execute("SELECT market, qty FROM paper_trades WHERE id=%s",
                            (res.trade_id,)).fetchone()
        assert row["market"] == "CRYPTO"
        assert Decimal(str(row["qty"])) > 0              # fractional sizing worked
        # IN book is untouched; crypto book now has locked capital.
        assert wl.competitor_wallet_state(cid, "IN").locked_in_open == Decimal("0")
        assert wl.competitor_wallet_state(cid, "CRYPTO").locked_in_open > 0
    finally:
        _wipe()


def test_in_path_unchanged_default_market():
    cid = _mkcompetitor()
    try:
        res = ex.execute_competitor_open(
            cid, "RELIANCE", "BUY", Decimal("1000"), Decimal("990"), Decimal("1030"),
            qty=5, actor=cid, rationale="t",          # no market kwarg → defaults IN
        )
        assert res.ok, res.message
        with conn() as c:
            row = c.execute("SELECT market FROM paper_trades WHERE id=%s",
                            (res.trade_id,)).fetchone()
        assert row["market"] == "IN"
    finally:
        _wipe()

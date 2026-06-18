"""
Markets — per-market books (multi-market UI). Read-only.

A NEW page (does not touch the IN-era pages, so India behaviour is unchanged).
Pick a market (India / US / Crypto) to see its own wallet, open positions, recent
closed trades, and the lessons the house agent is applying there. Degrades to an
info note if the multi-market migration hasn't run yet.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from helm.config import HOUSE_TRADE_FILTER
from helm.dashboard.format import wrapped_table
from helm.dashboard.theme import apply_theme, kpi_card, kpi_grid, page_header
from helm.data.store import conn
from helm.markets import all_markets
from helm.wallet import wallet_state

st.set_page_config(page_title="Helm — Markets", page_icon="🌐", layout="wide")
apply_theme()
page_header("Markets", "Per-market books — India · US · Crypto (read-only)", icon="🌐")

_CCY = {"INR": "₹", "USD": "$"}
_markets = all_markets()
_key = st.selectbox("Market", list(_markets),
                    format_func=lambda k: f"{_markets[k].name}  ·  {_markets[k].currency}")
_m = _markets[_key]
_sym = _CCY.get(_m.currency, "")

try:
    ws = wallet_state(_key)
    kpi_grid([
        kpi_card("Equity", f"{_sym}{ws.equity:,.2f}", icon="wallet"),
        kpi_card("Available", f"{_sym}{ws.available:,.2f}", icon="bank"),
        kpi_card("Realised P&L", f"{_sym}{ws.realised_net_pnl:,.2f}", icon="bar",
                 sub=("in profit" if ws.realised_net_pnl >= 0 else "in drawdown"),
                 sub_kind="pos" if ws.realised_net_pnl >= 0 else "neg"),
        kpi_card("Locked in open", f"{_sym}{ws.locked_in_open:,.2f}", icon="lock"),
    ])
    if _m.calendar.square_off_at() is None:
        st.caption("24/7 venue — no end-of-day square-off; positions time-stop instead.")

    with conn() as c:
        opens = pd.DataFrame(c.execute(
            f"SELECT symbol, side, qty, entry_price, stop_loss, target FROM paper_trades "
            f"WHERE status='OPEN' AND market=%s AND {HOUSE_TRADE_FILTER} ORDER BY entry_ts DESC",
            (_key,)).fetchall())
        closed = pd.DataFrame(c.execute(
            f"SELECT symbol, side, qty, entry_price, exit_price, exit_reason, "
            f"net_pnl_inr AS net, exit_ts FROM paper_trades "
            f"WHERE status='CLOSED' AND market=%s AND {HOUSE_TRADE_FILTER} "
            f"ORDER BY exit_ts DESC LIMIT 50", (_key,)).fetchall())

    st.markdown("#### Open positions")
    if not opens.empty:
        wrapped_table(opens)
    else:
        st.caption("No open positions.")
    st.markdown("#### Recent closed trades")
    if not closed.empty:
        wrapped_table(closed)
    else:
        st.caption("No closed trades in this market yet.")

    try:
        from helm.agents.instincts import lessons_for
        lessons = lessons_for("house", _key)
        if lessons:
            st.markdown("#### Lessons applied here")
            wrapped_table(pd.DataFrame([
                {"lesson": x["statement"], "layer": x.get("layer"),
                 "confidence": x.get("confidence"),
                 "scope": x.get("market") or "all markets"} for x in lessons]))
    except Exception:
        pass
except Exception as exc:  # noqa: BLE001 — read-only page; degrade, don't crash
    import psycopg
    if isinstance(exc, (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn)):
        st.info("Multi-market schema not present yet — run "
                "`scripts/migrate_multimarket.py` first.")
    else:
        st.exception(exc)   # surface the real error instead of masking it

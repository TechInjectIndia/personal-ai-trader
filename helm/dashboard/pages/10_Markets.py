"""
Markets — per-market books (multi-market UI). Read-only.

A NEW page (does not touch the IN-era pages, so India behaviour is unchanged).
Pick a market (India / US / Crypto) to see its own wallet, open positions, recent
closed trades, and the lessons the house agent is applying there. Degrades to an
info note if the multi-market migration hasn't run yet.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from helm.config import HOUSE_TRADE_FILTER
from helm.dashboard import trade_explorer as te
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
    _cal = _m.calendar
    _hrs = _cal.session_hours()
    if _hrs is None:
        st.markdown(f"**Hours** · 24/7 ({_cal.tz_label}) · 🟢 always open · "
                    "no EOD square-off (positions time-stop instead)")
    else:
        _o, _c = _hrs
        _sq = _cal.square_off_at()
        _line = f"**Hours** · {_o.strftime('%H:%M')}–{_c.strftime('%H:%M')} {_cal.tz_label}"
        if _sq is not None:
            _line += f" · square-off {_sq.strftime('%H:%M')}"
        _line += f" · {'🟢 Open now' if _cal.is_market_open() else '⚪ Closed'}"
        st.markdown(_line)

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

    # --- Trade explorer: every agent × every market, filterable + sortable ---
    st.divider()
    st.markdown("### Trade explorer")
    st.caption("Closed trades across every agent and market — filter and sort.")

    with conn() as c:
        agent_opts = te.agent_options(c)
    agent_labels = dict(agent_opts)
    mkt_opts = [(te.ALL, "All markets")] + [(k, m.name) for k, m in _markets.items()]
    mkt_labels = dict(mkt_opts)

    f1, f2, f3, f4, f5 = st.columns([1.5, 1.3, 1, 1.2, 0.8])
    with f1:
        sel_agent = st.selectbox("Agent", [v for v, _ in agent_opts],
                                 format_func=lambda v: agent_labels[v], key="te_agent")
    with f2:
        sel_mkt = st.selectbox("Market", [v for v, _ in mkt_opts],
                               format_func=lambda v: mkt_labels[v], key="te_market")
    with f3:
        sel_period = st.selectbox("Time frame", list(te.PERIODS), index=1, key="te_period")
    with f4:
        sel_sort = st.selectbox("Sort by", list(te.SORTS), key="te_sort")
    with f5:
        sel_desc = st.toggle("Desc", value=True, key="te_desc")

    cutoff = te.period_cutoff(sel_period, datetime.now(te.IST))
    with conn() as c:
        rows = te.query_trades(c, sel_agent, sel_mkt, cutoff)

    if not rows:
        st.caption("No closed trades match these filters.")
    else:
        recs = []
        for r in rows:
            pct = te.pct_net(r["net"], r["entry_price"], r["qty"])
            mkt = _markets.get(r["market"])
            csym = _CCY.get(mkt.currency, "") if mkt else ""
            recs.append({
                "Time": r["exit_ts"].astimezone(te.IST).strftime("%Y-%m-%d %H:%M"),
                "Agent": te.agent_label(r["competitor_id"], agent_labels),
                "Market": r["market"],
                "Symbol": r["symbol"],
                "Side": r["side"],
                "Qty": float(r["qty"]) if r["qty"] is not None else None,
                "Entry": float(r["entry_price"]) if r["entry_price"] is not None else None,
                "Exit": float(r["exit_price"]) if r["exit_price"] is not None else None,
                "Net P&L": f"{csym}{float(r['net']):,.2f}" if r["net"] is not None else "—",
                "% Net": round(pct, 2) if pct is not None else None,
                "Reason": r["exit_reason"],
                "Strategy": r["strategy"],
                "pct": pct if pct is not None else float("-inf"),   # hidden sort keys
                "net": float(r["net"]) if r["net"] is not None else float("-inf"),
                "ts": r["exit_ts"],
            })
        wins = sum(1 for r in rows if (r["net"] or 0) > 0)
        summary = f"{len(rows)} trades · win {100 * wins / len(rows):.0f}%"
        if sel_mkt != te.ALL:
            net_sum = sum(float(r["net"] or 0) for r in rows)
            summary += f" · net {_CCY.get(_markets[sel_mkt].currency, '')}{net_sum:,.2f}"
        st.caption(summary)
        df = pd.DataFrame(recs).sort_values(te.SORTS[sel_sort],
                                            ascending=not sel_desc, kind="stable")
        wrapped_table(df.drop(columns=["pct", "net", "ts"]))
except Exception as exc:  # noqa: BLE001 — read-only page; degrade, don't crash
    import psycopg
    if isinstance(exc, (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn)):
        st.info("Multi-market schema not present yet — run "
                "`scripts/migrate_multimarket.py` first.")
    else:
        st.exception(exc)   # surface the real error instead of masking it

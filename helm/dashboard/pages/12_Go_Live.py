"""
Go-Live readiness (multi-market UI). Read-only.

Shows the latest per-market verdict from `go_live_readiness` (written by
scripts/go_live_readiness.py): a market is READY only when its forward paper book
AND its backtests are both net-positive. Funding stays a manual human action —
nothing on this page arms live trading.
"""

from __future__ import annotations

import json
import subprocess
import sys

import streamlit as st

from helm.dashboard.theme import apply_theme, kpi_card, kpi_grid, page_header
from helm.data.store import conn
from helm.markets import all_markets

st.set_page_config(page_title="Helm — Go-Live", page_icon="🚦", layout="wide")
apply_theme()
page_header("Go-Live Readiness",
            "Fund a market only when paper AND backtest are positive", icon="🚦")
st.caption("Read-only verdict. Funding is a manual human action — nothing here arms live trading. "
           "Re-runs automatically daily after close; use the button to refresh now.")

if st.button("↻ Re-run readiness now"):
    with st.spinner("Evaluating all markets (paper + backtest gates)…"):
        proc = subprocess.run([sys.executable, "scripts/go_live_readiness.py", "--persist"],
                              capture_output=True, text=True, timeout=180)
    st.code((proc.stdout or proc.stderr or "(no output)")[-2000:])
    st.rerun()


def _reasons(val) -> list:
    if isinstance(val, list):
        return val
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


try:
    with conn() as c:
        rows = list(c.execute(
            "SELECT DISTINCT ON (market) market, ready, paper_pass, backtest_pass, "
            "reasons, evaluated_ts FROM go_live_readiness "
            "ORDER BY market, evaluated_ts DESC"))
    latest = {r["market"]: r for r in rows}

    cards = []
    for k, m in all_markets().items():
        r = latest.get(k)
        if r is None:
            cards.append(kpi_card(m.name, "no run", icon="clock",
                                  sub="run go_live_readiness.py", sub_kind="muted"))
            continue
        ready = bool(r["ready"])
        tick = lambda ok: "✓" if ok else "✗"  # noqa: E731
        cards.append(kpi_card(
            m.name, "READY ✅" if ready else "not ready",
            icon="rocket" if ready else "lock",
            sub=f"paper {tick(r['paper_pass'])} · backtest {tick(r['backtest_pass'])}",
            sub_kind="pos" if ready else "warn"))
    kpi_grid(cards, cols=len(cards) or 1)

    blocked = [(k, _reasons(latest[k]["reasons"]))
               for k in all_markets() if latest.get(k) and not latest[k]["ready"]]
    if blocked:
        st.markdown("#### Why not ready")
        for k, reasons in blocked:
            st.markdown(f"**{k}** — " + ("; ".join(reasons) if reasons else "—"))
    if not rows:
        st.info("No readiness runs recorded yet. Run: "
                "`python scripts/go_live_readiness.py --persist`")
except Exception as exc:  # noqa: BLE001 — read-only page; degrade, don't crash
    import psycopg
    if isinstance(exc, (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn)):
        st.info("Go-live readiness not available yet — run "
                "`scripts/migrate_multimarket.py` first.")
    else:
        st.exception(exc)   # surface the real error instead of masking it

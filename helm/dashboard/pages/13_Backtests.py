"""
Backtest evidence browser (multi-market UI). Read-only.

Latest forward backtest per market × strategy × symbol from `backtest_runs`
(written by scripts/run_backtest_sweep.py / run_backtest.py). Records the true
window depth so a thin yfinance window isn't mistaken for a deep one.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from helm.dashboard.format import wrapped_table
from helm.dashboard.theme import apply_theme, page_header
from helm.data.store import conn

st.set_page_config(page_title="Helm — Backtests", page_icon="🧪", layout="wide")
apply_theme()
page_header("Backtest Evidence",
            "Latest forward backtest per market × strategy × symbol", icon="🧪")

try:
    with conn() as c:
        rows = list(c.execute(
            "SELECT DISTINCT ON (market, strategy, symbol) market, strategy, symbol, "
            "bar_minutes, n_signals, n_trades, metrics, start_ts, end_ts, created_ts "
            "FROM backtest_runs ORDER BY market, strategy, symbol, created_ts DESC"))
    if not rows:
        st.info("No backtests recorded yet. Run: "
                "`python scripts/run_backtest_sweep.py --persist`")
    else:
        df = pd.DataFrame([{
            "market": r["market"], "strategy": r["strategy"], "symbol": r["symbol"],
            "bar": f"{r['bar_minutes']}m",
            "signals": r["n_signals"], "trades": r["n_trades"],
            "net": (r["metrics"] or {}).get("net"),
            "win%": (r["metrics"] or {}).get("win_pct"),
            "window_days": (r["end_ts"] - r["start_ts"]).days,
            "as_of": r["created_ts"].strftime("%Y-%m-%d %H:%M"),
        } for r in rows])
        wrapped_table(df.sort_values("net", ascending=False, na_position="last"),
                      right_align={"signals", "trades", "net", "win%", "window_days"})
        st.caption("Deterministic, decider-off backtests. Short windows (esp. yfinance "
                   "intraday for IN/US) mean sparse trades — read alongside the window_days.")
except Exception as exc:  # noqa: BLE001 — read-only page; degrade, don't crash
    import psycopg
    if isinstance(exc, (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn)):
        st.info("Backtest evidence not available yet — run "
                "`scripts/migrate_multimarket.py` first.")
    else:
        st.exception(exc)   # surface the real error instead of masking it

"""
Summary page — performance dashboard for the paper-trading bot.

Streamlit auto-discovers files under `dashboard/pages/`; the leading `2_`
controls sort order in the sidebar. The main app.py page is `1_` implicitly.

What's here:
  - Headline metrics: total closed trades, hit rate, expectancy, gross/net P&L
  - Cumulative P&L curve over time
  - Per-strategy and per-symbol breakdowns (count, win rate, avg P&L)
  - Decider scoreboard: how often each actor TAKES vs SKIPS
  - Trade-by-trade table for any custom slicing the user wants

All numbers are computed from `paper_trades` (closed only) and `decisions`
in Postgres. No caching of P&L — these are fast queries on a small table.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from helm.dashboard.format import fmt_ist, wrapped_table
from helm.data.store import conn

st.set_page_config(page_title="Helm — Summary", page_icon="📊", layout="wide")
st.title("Helm — performance summary")

# ───────────────────────── filters ─────────────────────────
today = date.today()
default_from = today - timedelta(days=30)
col_a, col_b, col_c = st.columns([1, 1, 2])
date_from = col_a.date_input("From", default_from)
date_to = col_b.date_input("To", today)
strategy_filter = col_c.text_input("Strategy filter (substring, optional)", "")

if date_from > date_to:
    st.error("'From' date must be on or before 'To' date.")
    st.stop()

# ───────────────────────── pull data ─────────────────────────
with conn() as c:
    closed_rows = list(c.execute(
        """
        SELECT
            pt.id, pt.symbol, pt.side, pt.qty,
            pt.entry_price, pt.exit_price,
            pt.entry_ts, pt.exit_ts,
            pt.exit_reason, pt.pnl_inr,
            d.actor, d.verdict, d.reasoning,
            s.strategy
        FROM paper_trades pt
        LEFT JOIN decisions d ON d.id = pt.decision_id
        LEFT JOIN signals s ON s.id = d.signal_id
        WHERE pt.status = 'CLOSED'
          AND pt.exit_ts::date BETWEEN %s AND %s
        ORDER BY pt.exit_ts DESC
        """,
        (date_from, date_to),
    ))
    decision_rows = list(c.execute(
        """
        SELECT d.actor, d.verdict, COUNT(*) AS n
        FROM decisions d
        WHERE d.ts::date BETWEEN %s AND %s
        GROUP BY d.actor, d.verdict
        ORDER BY d.actor, d.verdict
        """,
        (date_from, date_to),
    ))

if strategy_filter:
    closed_rows = [r for r in closed_rows if strategy_filter.lower() in (r["strategy"] or "").lower()]

if not closed_rows:
    st.info("No closed trades in this window.")
    st.stop()

df = pd.DataFrame(closed_rows)
df["pnl_inr"] = df["pnl_inr"].astype(float)
df["entry_price"] = df["entry_price"].astype(float)
df["exit_price"] = df["exit_price"].astype(float)
df["exit_ts"] = pd.to_datetime(df["exit_ts"])
df["entry_ts"] = pd.to_datetime(df["entry_ts"])
df["is_win"] = df["pnl_inr"] > 0
# Pre-format an IST display column for tables; raw entry_ts kept for charts/sort.
df["entry_ts_fmt"] = df["entry_ts"].apply(fmt_ist)

# ───────────────────────── headline metrics ─────────────────────────
n_trades = len(df)
wins = int(df["is_win"].sum())
losses = n_trades - wins
hit_rate = wins / n_trades if n_trades else 0
gross_pnl = df["pnl_inr"].sum()
avg_win = df.loc[df["is_win"], "pnl_inr"].mean() if wins else 0
avg_loss = df.loc[~df["is_win"], "pnl_inr"].mean() if losses else 0
expectancy = (hit_rate * (avg_win or 0)) + ((1 - hit_rate) * (avg_loss or 0))

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Trades", n_trades)
m2.metric("Hit rate", f"{hit_rate*100:.1f}%", f"{wins}W · {losses}L")
m3.metric("Gross P&L", f"₹{gross_pnl:,.2f}")
m4.metric("Avg win / loss", f"₹{(avg_win or 0):.2f} / ₹{(avg_loss or 0):.2f}")
m5.metric("Expectancy / trade", f"₹{expectancy:.2f}")

st.divider()

# ───────────────────────── cumulative P&L curve ─────────────────────────
st.subheader("Cumulative P&L")
curve = df.sort_values("exit_ts").copy()
curve["cum_pnl"] = curve["pnl_inr"].cumsum()
chart_df = curve.set_index("exit_ts")[["cum_pnl"]].rename(columns={"cum_pnl": "Cumulative ₹"})
st.line_chart(chart_df, height=260)

# ───────────────────────── breakdowns ─────────────────────────
st.subheader("Breakdowns")
left, right = st.columns(2)

with left:
    st.caption("By strategy")
    by_strat = df.groupby("strategy", dropna=False).agg(
        trades=("id", "count"),
        wins=("is_win", "sum"),
        pnl=("pnl_inr", "sum"),
        avg_pnl=("pnl_inr", "mean"),
    ).reset_index()
    by_strat["hit_rate"] = (by_strat["wins"] / by_strat["trades"] * 100).round(1)
    by_strat = by_strat[["strategy", "trades", "hit_rate", "pnl", "avg_pnl"]]
    by_strat.columns = ["Strategy", "Trades", "Hit %", "P&L (₹)", "Avg/trade (₹)"]
    wrapped_table(by_strat)

with right:
    st.caption("By symbol")
    by_sym = df.groupby("symbol").agg(
        trades=("id", "count"),
        wins=("is_win", "sum"),
        pnl=("pnl_inr", "sum"),
    ).reset_index()
    by_sym["hit_rate"] = (by_sym["wins"] / by_sym["trades"] * 100).round(1)
    by_sym = by_sym[["symbol", "trades", "hit_rate", "pnl"]]
    by_sym.columns = ["Symbol", "Trades", "Hit %", "P&L (₹)"]
    wrapped_table(by_sym.sort_values("P&L (₹)", ascending=False))

# ───────────────────────── exit reason breakdown ─────────────────────────
st.subheader("Exit reasons")
exit_df = df.groupby("exit_reason").agg(
    trades=("id", "count"),
    pnl=("pnl_inr", "sum"),
    avg=("pnl_inr", "mean"),
).reset_index()
exit_df.columns = ["Why", "Trades", "P&L (₹)", "Avg (₹)"]
wrapped_table(exit_df)

# ───────────────────────── decider scoreboard ─────────────────────────
st.subheader("Decider activity")
if decision_rows:
    dec_df = pd.DataFrame(decision_rows)
    pivot = dec_df.pivot_table(index="actor", columns="verdict", values="n",
                               fill_value=0, aggfunc="sum").reset_index()
    pivot["total"] = pivot.drop(columns=["actor"]).sum(axis=1)
    if "TAKE" in pivot.columns:
        pivot["take_rate"] = (pivot["TAKE"] / pivot["total"] * 100).round(1)
    wrapped_table(pivot)
else:
    st.caption("No decisions recorded in this window.")

# ───────────────────────── best / worst ─────────────────────────
st.subheader("Best & worst trades")
b1, b2 = st.columns(2)
keep_cols = ["entry_ts_fmt", "symbol", "strategy", "side", "qty",
             "entry_price", "exit_price", "exit_reason", "pnl_inr"]
disp_cols = ["At", "Symbol", "Strategy", "Side", "Qty", "Entry", "Exit", "Why", "P&L (₹)"]

with b1:
    st.caption("Top 5 winners")
    top = df.sort_values("pnl_inr", ascending=False).head(5)[keep_cols].copy()
    top.columns = disp_cols
    wrapped_table(top)

with b2:
    st.caption("Top 5 losers")
    bot = df.sort_values("pnl_inr", ascending=True).head(5)[keep_cols].copy()
    bot.columns = disp_cols
    wrapped_table(bot)

# ───────────────────────── full trade log ─────────────────────────
with st.expander(f"All {n_trades} trades in window"):
    show = df[keep_cols + ["actor", "reasoning"]].copy()
    show.columns = disp_cols + ["Actor", "Reasoning"]
    wrapped_table(show)

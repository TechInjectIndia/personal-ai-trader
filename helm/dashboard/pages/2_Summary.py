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

from helm.config import HOUSE_TRADE_FILTER
from helm.dashboard.format import fmt_ist_short, wrapped_table
from helm.dashboard.theme import apply_theme, kpi_card, kpi_grid, page_header
from helm.data.store import conn

# Multi-agent: an "Agent" selector scopes the page to All agents, the House bot,
# or any single competitor. The House option keeps the legacy semantics (NULL or
# 'house-claude') so it stays consistent with the wallet on the main page; other
# competitors match their id exactly. Qualify the filter per table alias so it
# isn't ambiguous across the paper_trades⋈decisions⋈signals join.
_HOUSE_PT = HOUSE_TRADE_FILTER.replace("competitor_id", "pt.competitor_id")
_HOUSE_D = HOUSE_TRADE_FILTER.replace("competitor_id", "d.competitor_id")
SCOPE_ALL = "__all__"

st.set_page_config(page_title="Helm — Summary", page_icon="📊", layout="wide")
apply_theme()
page_header("Performance Summary",
            "Per-agent P&L, win rate and decider activity", icon="📊")

# ───────────────────────── filters ─────────────────────────
with conn() as c:
    _comp_rows = list(c.execute(
        "SELECT id, name FROM competitors ORDER BY (autonomy_level <> 'incumbent'), name"
    ))
_comp_name = {r["id"]: r["name"] for r in _comp_rows}


def _agent_label(opt: str) -> str:
    return "All agents" if opt == SCOPE_ALL else _comp_name.get(opt, opt)


def _agent_name(cid: "str | None") -> str:
    if cid is None or cid == "house-claude":
        return _comp_name.get("house-claude", "House (Claude)")
    return _comp_name.get(cid, cid)


today = date.today()
default_from = today - timedelta(days=30)
col_a, col_b, col_c, col_d = st.columns([1, 1, 1.3, 1.5])
date_from = col_a.date_input("From", default_from)
date_to = col_b.date_input("To", today)
agent_sel = col_c.selectbox("Agent", [SCOPE_ALL] + [r["id"] for r in _comp_rows],
                            format_func=_agent_label)
strategy_filter = col_d.text_input("Strategy filter (substring, optional)", "")
show_agent = agent_sel == SCOPE_ALL

if date_from > date_to:
    st.error("'From' date must be on or before 'To' date.")
    st.stop()

# ───────────────────────── pull data ─────────────────────────
# Scope predicate from the Agent selector (House keeps NULL-or-house-claude).
if agent_sel == SCOPE_ALL:
    pt_scope, d_scope, scope_params = "TRUE", "TRUE", []
elif agent_sel == "house-claude":
    pt_scope, d_scope, scope_params = _HOUSE_PT, _HOUSE_D, []
else:
    pt_scope, d_scope, scope_params = "pt.competitor_id = %s", "d.competitor_id = %s", [agent_sel]

with conn() as c:
    closed_rows = list(c.execute(
        f"""
        SELECT
            pt.id, pt.symbol, pt.side, pt.qty,
            pt.entry_price, pt.exit_price,
            pt.entry_ts, pt.exit_ts,
            pt.exit_reason, pt.pnl_inr, pt.competitor_id,
            d.actor, d.verdict, d.reasoning,
            s.strategy
        FROM paper_trades pt
        LEFT JOIN decisions d ON d.id = pt.decision_id
        LEFT JOIN signals s ON s.id = d.signal_id
        WHERE pt.status = 'CLOSED'
          AND pt.exit_ts::date BETWEEN %s AND %s
          AND {pt_scope}
        ORDER BY pt.exit_ts DESC
        """,
        [date_from, date_to, *scope_params],
    ))
    decision_rows = list(c.execute(
        f"""
        SELECT d.actor, d.verdict, COUNT(*) AS n
        FROM decisions d
        WHERE d.ts::date BETWEEN %s AND %s
          AND {d_scope}
        GROUP BY d.actor, d.verdict
        ORDER BY d.actor, d.verdict
        """,
        [date_from, date_to, *scope_params],
    ))

if strategy_filter:
    closed_rows = [r for r in closed_rows if strategy_filter.lower() in (r["strategy"] or "").lower()]

if not closed_rows:
    st.info(f"No closed trades for {_agent_label(agent_sel)} in this window.")
    st.stop()

df = pd.DataFrame(closed_rows)
df["pnl_inr"] = df["pnl_inr"].astype(float)
df["entry_price"] = df["entry_price"].astype(float)
df["exit_price"] = df["exit_price"].astype(float)
df["exit_ts"] = pd.to_datetime(df["exit_ts"])
df["entry_ts"] = pd.to_datetime(df["entry_ts"])
df["is_win"] = df["pnl_inr"] > 0
# Pre-format a compact IST display column for tables; raw entry_ts kept for
# charts/sort. Short form ("06 May 02:32 PM") keeps dense cells on few lines.
df["entry_ts_fmt"] = df["entry_ts"].apply(fmt_ist_short)
df["agent"] = df["competitor_id"].map(_agent_name)

# ───────────────────────── headline metrics ─────────────────────────
n_trades = len(df)
wins = int(df["is_win"].sum())
losses = n_trades - wins
hit_rate = wins / n_trades if n_trades else 0
gross_pnl = df["pnl_inr"].sum()
avg_win = df.loc[df["is_win"], "pnl_inr"].mean() if wins else 0
avg_loss = df.loc[~df["is_win"], "pnl_inr"].mean() if losses else 0
expectancy = (hit_rate * (avg_win or 0)) + ((1 - hit_rate) * (avg_loss or 0))

kpi_grid([
    kpi_card("Trades", f"{n_trades:,}", icon="repeat"),
    kpi_card("Hit rate", f"{hit_rate*100:.1f}%", icon="target",
             sub=f"{wins}W · {losses}L", sub_kind="pos" if wins >= losses else "neg"),
    kpi_card("Gross P&L", f"₹{gross_pnl:,.2f}", icon="wallet",
             sub="before charges", sub_kind="pos" if gross_pnl >= 0 else "neg"),
    kpi_card("Avg win / loss", f"₹{(avg_win or 0):,.2f} / ₹{(avg_loss or 0):,.2f}",
             icon="scale"),
    kpi_card("Expectancy / trade", f"₹{expectancy:,.2f}", icon="trend-up",
             sub="per trade", sub_kind="muted"),
], cols=5)
st.caption(f"Showing **{_agent_label(agent_sel)}** · {n_trades:,} closed trades "
           f"from {date_from:%d %b %Y} to {date_to:%d %b %Y}.")

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
    by_strat["pnl"] = by_strat["pnl"].round(2)
    by_strat["avg_pnl"] = by_strat["avg_pnl"].round(2)
    by_strat = by_strat[["strategy", "trades", "hit_rate", "pnl", "avg_pnl"]]
    by_strat.columns = ["Strategy", "Trades", "Hit %", "P&L (₹)", "Avg/trade (₹)"]
    wrapped_table(by_strat,
                  right_align=["Trades", "Hit %", "P&L (₹)", "Avg/trade (₹)"])

with right:
    st.caption("By symbol")
    by_sym = df.groupby("symbol").agg(
        trades=("id", "count"),
        wins=("is_win", "sum"),
        pnl=("pnl_inr", "sum"),
    ).reset_index()
    by_sym["hit_rate"] = (by_sym["wins"] / by_sym["trades"] * 100).round(1)
    by_sym["pnl"] = by_sym["pnl"].round(2)
    by_sym = by_sym[["symbol", "trades", "hit_rate", "pnl"]]
    by_sym.columns = ["Symbol", "Trades", "Hit %", "P&L (₹)"]
    wrapped_table(by_sym.sort_values("P&L (₹)", ascending=False),
                  right_align=["Trades", "Hit %", "P&L (₹)"])

# ───────────────────────── exit reason breakdown ─────────────────────────
st.subheader("Exit reasons")
exit_df = df.groupby("exit_reason").agg(
    trades=("id", "count"),
    pnl=("pnl_inr", "sum"),
    avg=("pnl_inr", "mean"),
).reset_index()
exit_df["pnl"] = exit_df["pnl"].round(2)
exit_df["avg"] = exit_df["avg"].round(2)
exit_df.columns = ["Why", "Trades", "P&L (₹)", "Avg (₹)"]
wrapped_table(exit_df, right_align=["Trades", "P&L (₹)", "Avg (₹)"])

# ───────────────────────── decider scoreboard ─────────────────────────
st.subheader("Decider activity")
if decision_rows:
    dec_df = pd.DataFrame(decision_rows)
    pivot = dec_df.pivot_table(index="actor", columns="verdict", values="n",
                               fill_value=0, aggfunc="sum").reset_index()
    pivot["total"] = pivot.drop(columns=["actor"]).sum(axis=1)
    if "TAKE" in pivot.columns:
        pivot["take_rate"] = (pivot["TAKE"] / pivot["total"] * 100).round(1)
    pivot = pivot.rename(columns={"actor": "Actor", "total": "Total",
                                  "take_rate": "Take %"})
    wrapped_table(pivot, right_align=[c for c in pivot.columns if c != "Actor"])
else:
    st.caption("No decisions recorded in this window.")

# ───────────────────────── best / worst ─────────────────────────
st.subheader("Best & worst trades")
keep_cols = ["entry_ts_fmt", "symbol", "strategy", "side", "qty",
             "entry_price", "exit_price", "exit_reason", "pnl_inr"]
disp_cols = ["At", "Symbol", "Strategy", "Side", "Qty", "Entry", "Exit", "Why", "P&L (₹)"]
num_cols = ["Qty", "Entry", "Exit", "P&L (₹)"]
if show_agent:  # only meaningful when more than one agent is in view
    keep_cols = ["agent"] + keep_cols
    disp_cols = ["Agent"] + disp_cols


def _fmt_trades(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame[keep_cols].copy()
    out.columns = disp_cols
    out["Entry"] = out["Entry"].map(lambda v: f"{v:,.2f}")
    out["Exit"] = out["Exit"].map(lambda v: f"{v:,.2f}")
    out["P&L (₹)"] = out["P&L (₹)"].map(lambda v: f"{v:,.2f}")
    return out


# Full-width, stacked. (Previously two half-width tables that crushed each
# cell to one character per line.) Winners first, then losers.
st.caption("Top 5 winners")
wrapped_table(_fmt_trades(df.sort_values("pnl_inr", ascending=False).head(5)),
              right_align=num_cols, single_line=True)
st.caption("Top 5 losers")
wrapped_table(_fmt_trades(df.sort_values("pnl_inr", ascending=True).head(5)),
              right_align=num_cols, single_line=True)

# ───────────────────────── full trade log ─────────────────────────
with st.expander(f"All {n_trades} trades in window"):
    show = _fmt_trades(df)            # df is already exit_ts DESC from the query
    if not show_agent:               # in all-agents view the Agent column covers attribution
        show["Actor"] = df["actor"].values
    # Cap the free-text reasoning so one verbose row can't balloon the table
    # height; clamp_cols also bounds the column width. Full reasoning lives on
    # the Activity Log / Retrospectives pages.
    show["Reasoning"] = df["reasoning"].map(
        lambda r: (r[:200].rstrip() + "…") if r and len(r) > 200 else (r or "")
    ).values
    wrapped_table(show, right_align=num_cols, clamp_cols={"Reasoning": 440},
                  single_line=True)

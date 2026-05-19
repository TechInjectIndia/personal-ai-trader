"""
Self-Improvement Loop page — observability for the PM / Engineer / Tester
autonomy pipeline.

Five panels:
  1. Goal progress — equity curve from metrics_snapshots, vs initial & goal
  2. Queue depths — agent_tasks by status + improvement_proposals by status
  3. Releases timeline — every commit shipped by the engineer, with status
  4. Recent agent_runs — per-agent latency, outcome, links to traces
  5. Open tasks table — what's queued / in-progress, with claim age
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from helm.data.store import conn

st.set_page_config(page_title="Helm — Self-Improvement Loop", page_icon="🔁", layout="wide")
st.title("Self-Improvement Loop")
st.caption("PM picks → Engineer ships → Tester verifies. Everything observable in one place.")


# ─── data fetch helpers ────────────────────────────────────────────────

def _latest_snapshot() -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM metrics_snapshots ORDER BY snapshot_ts DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _snapshot_history(days: int = 60) -> pd.DataFrame:
    with conn() as c:
        rows = list(c.execute(
            "SELECT snapshot_ts, equity_inr, progress_pct, win_rate_pct, "
            "       max_drawdown_inr, proposals_open, tasks_open, "
            "       releases_unverified, trades_total "
            "FROM metrics_snapshots "
            "WHERE snapshot_ts >= now() - make_interval(days => %s) "
            "ORDER BY snapshot_ts ASC",
            (days,),
        ))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["snapshot_ts"] = pd.to_datetime(df["snapshot_ts"])
    return df


def _agent_tasks(limit: int = 25) -> pd.DataFrame:
    with conn() as c:
        rows = list(c.execute(
            "SELECT id, created_ts, created_by, task_type, status, priority, "
            "       title, claimed_by, claimed_ts, release_id, error "
            "FROM agent_tasks ORDER BY created_ts DESC LIMIT %s",
            (limit,),
        ))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _releases(limit: int = 20) -> pd.DataFrame:
    with conn() as c:
        rows = list(c.execute(
            "SELECT r.id, r.created_ts, r.commit_sha, r.status, r.summary, "
            "       r.verified_ts, r.reverted_ts, r.task_id, t.title AS task_title "
            "FROM releases r LEFT JOIN agent_tasks t ON t.id = r.task_id "
            "ORDER BY r.created_ts DESC LIMIT %s",
            (limit,),
        ))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["commit_sha"] = df["commit_sha"].str.slice(0, 10)
    return df


def _agent_runs(limit: int = 30) -> pd.DataFrame:
    with conn() as c:
        rows = list(c.execute(
            "SELECT id, started_ts, finished_ts, agent, invocation, "
            "       outcome, latency_ms, task_id, release_id, summary "
            "FROM agent_runs ORDER BY started_ts DESC LIMIT %s",
            (limit,),
        ))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _proposals_open_by_category() -> pd.DataFrame:
    with conn() as c:
        rows = list(c.execute(
            "SELECT category, COUNT(*) AS n FROM improvement_proposals "
            "WHERE status='open' GROUP BY category ORDER BY n DESC"
        ))
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["category", "n"])


def _autonomy_paused() -> bool:
    with conn() as c:
        r = c.execute(
            "SELECT value FROM settings WHERE key='autonomy_paused'"
        ).fetchone()
    if not r:
        return False
    v = r["value"]
    return v in (True, "true", "True", 1, "1")


# ─── render ────────────────────────────────────────────────────────────

paused = _autonomy_paused()
if paused:
    st.error("⏸️  Autonomy paused — Tester or human halted the loop. "
             "Check agent_runs for the cause.")

latest = _latest_snapshot()
if not latest:
    st.warning("No metrics snapshots yet. The first one lands at 15:35 IST today; "
               "run `python scripts/snapshot_metrics.py` for an immediate sample.")
    st.stop()

# ─── 1. Goal progress ─────────────────────────────────────────────────
st.subheader("Goal progress")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Equity", f"₹{float(latest['equity_inr']):,.0f}",
          delta=f"₹{float(latest['realised_net_pnl_inr']):,.0f} realised")
c2.metric("Progress to goal", f"{float(latest['progress_pct']):.1f}%")
c3.metric("Win rate",
          f"{float(latest['win_rate_pct'] or 0):.0f}%",
          delta=f"{latest['trades_total']} trades")
c4.metric("Max drawdown", f"₹{float(latest['max_drawdown_inr']):,.0f}")

hist = _snapshot_history()
if not hist.empty:
    initial = float(latest["initial_capital_inr"])
    goal = float(latest["goal_capital_inr"])
    chart_df = hist[["snapshot_ts", "equity_inr"]].copy()
    chart_df["equity_inr"] = chart_df["equity_inr"].astype(float)
    chart_df["initial"] = initial
    chart_df["goal"] = goal
    chart_df = chart_df.set_index("snapshot_ts")
    st.line_chart(chart_df, height=240)

# ─── 2. Queue depths ──────────────────────────────────────────────────
st.subheader("Queues")
qcol1, qcol2, qcol3 = st.columns(3)
qcol1.metric("Improvement proposals (open)", latest["proposals_open"])
qcol2.metric("Agent tasks (open/in-progress)", latest["tasks_open"])
qcol3.metric("Releases unverified", latest["releases_unverified"])

prop_df = _proposals_open_by_category()
if not prop_df.empty:
    st.caption("Open proposals by category")
    st.bar_chart(prop_df.set_index("category"))

# ─── 3. Releases timeline ─────────────────────────────────────────────
st.subheader("Releases")
rel_df = _releases()
if rel_df.empty:
    st.caption("No releases yet — the Engineer agent ships them as tasks complete.")
else:
    rel_df["age"] = (pd.Timestamp.now(tz="UTC") - pd.to_datetime(rel_df["created_ts"]))
    rel_df["age"] = rel_df["age"].dt.total_seconds().apply(_pretty_age := (lambda s: (
        f"{int(s//3600)}h" if s >= 3600 else f"{int(s//60)}m"
    )))
    rel_view = rel_df[["id", "created_ts", "commit_sha", "status", "task_title",
                       "summary", "age"]].rename(columns={
        "id": "rel", "created_ts": "shipped", "task_title": "task",
        "commit_sha": "sha",
    })
    st.dataframe(rel_view, use_container_width=True, hide_index=True)

# ─── 4. Agent runs ────────────────────────────────────────────────────
st.subheader("Recent agent runs")
runs_df = _agent_runs()
if runs_df.empty:
    st.caption("No agent runs yet.")
else:
    runs_view = runs_df[["started_ts", "agent", "invocation", "outcome",
                         "latency_ms", "task_id", "release_id", "summary"]]
    st.dataframe(runs_view, use_container_width=True, hide_index=True)

# ─── 5. Open tasks table ──────────────────────────────────────────────
st.subheader("Open / in-progress tasks")
tasks_df = _agent_tasks()
if tasks_df.empty:
    st.caption("Queue empty.")
else:
    active = tasks_df[tasks_df["status"].isin(
        ["open", "in_progress", "needs_human"])]
    if active.empty:
        st.caption("No active tasks. Recent completed:")
        st.dataframe(tasks_df.head(10), use_container_width=True, hide_index=True)
    else:
        st.dataframe(active, use_container_width=True, hide_index=True)

# ─── footer / hints ────────────────────────────────────────────────────
with st.expander("How this page is wired"):
    st.markdown(
        "- **Snapshots** are taken daily at 15:35 IST by `scripts/snapshot_metrics.py`.\n"
        "- **PM agent** runs Mondays 06:00 IST; queues 0–3 tasks per review.\n"
        "- **Engineer agent** polls every 30 min; one task per run; commits to `main`.\n"
        "- **Tester agent** is reactive — verifies each release, reverts on failure.\n"
        "- See `AGENTS/*.md` for role charters and `RELEASES.md` for the changelog.")

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

from helm.analytics.economics import book_economics
from helm.config import live_flag
from helm.dashboard.agents import HOUSE_ID, agent_label, agent_selectbox, scope_predicate
from helm.dashboard.format import fmt_ist_short, wrapped_table
from helm.dashboard.theme import apply_theme, page_header
from helm.data.store import conn

st.set_page_config(page_title="Helm — Self-Improvement Loop", page_icon="🔁", layout="wide")
apply_theme()
page_header("Self-Improvement Loop",
            "PM → Engineer → Tester: the autonomous build loop", icon="🔁")
st.caption("PM picks → Engineer ships → Tester verifies. Everything observable in one place.")


# ─── data fetch helpers ────────────────────────────────────────────────

def _latest_snapshot(agent_sel: str) -> dict | None:
    pred, params = scope_predicate(agent_sel)
    with conn() as c:
        row = c.execute(
            f"SELECT * FROM metrics_snapshots WHERE {pred} "
            "ORDER BY snapshot_ts DESC LIMIT 1",
            params,
        ).fetchone()
    return dict(row) if row else None


def _snapshot_history(agent_sel: str, days: int = 60) -> pd.DataFrame:
    pred, params = scope_predicate(agent_sel)
    with conn() as c:
        rows = list(c.execute(
            "SELECT snapshot_ts, equity_inr, progress_pct, win_rate_pct, "
            "       max_drawdown_inr, proposals_open, tasks_open, "
            "       releases_unverified, trades_total "
            "FROM metrics_snapshots "
            f"WHERE snapshot_ts >= now() - make_interval(days => %s) AND {pred} "
            "ORDER BY snapshot_ts ASC",
            (days, *params),
        ))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["snapshot_ts"] = pd.to_datetime(df["snapshot_ts"])
    return df


def _agent_tasks(agent_sel: str, limit: int = 25) -> pd.DataFrame:
    pred, params = scope_predicate(agent_sel)
    with conn() as c:
        rows = list(c.execute(
            "SELECT id, created_ts, created_by, task_type, status, priority, "
            "       title, claimed_by, claimed_ts, release_id, error "
            f"FROM agent_tasks WHERE {pred} ORDER BY created_ts DESC LIMIT %s",
            (*params, limit),
        ))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _releases(agent_sel: str, limit: int = 20) -> pd.DataFrame:
    pred, params = scope_predicate(agent_sel, alias="r")
    with conn() as c:
        rows = list(c.execute(
            "SELECT r.id, r.created_ts, r.commit_sha, r.status, r.summary, "
            "       r.verified_ts, r.reverted_ts, r.task_id, t.title AS task_title "
            "FROM releases r LEFT JOIN agent_tasks t ON t.id = r.task_id "
            f"WHERE {pred} ORDER BY r.created_ts DESC LIMIT %s",
            (*params, limit),
        ))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["commit_sha"] = df["commit_sha"].str.slice(0, 10)
    return df


def _agent_runs(agent_sel: str, limit: int = 30) -> pd.DataFrame:
    pred, params = scope_predicate(agent_sel)
    with conn() as c:
        rows = list(c.execute(
            "SELECT id, started_ts, finished_ts, agent, invocation, "
            "       outcome, latency_ms, task_id, release_id, summary "
            f"FROM agent_runs WHERE {pred} ORDER BY started_ts DESC LIMIT %s",
            (*params, limit),
        ))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _proposals_open_by_category(agent_sel: str) -> pd.DataFrame:
    pred, params = scope_predicate(agent_sel)
    with conn() as c:
        rows = list(c.execute(
            "SELECT category, COUNT(*) AS n FROM improvement_proposals "
            f"WHERE status='open' AND {pred} GROUP BY category ORDER BY n DESC",
            params,
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


def _proposals_live_count(agent_sel: str, status: str = "open") -> int:
    """Live count of improvement_proposals for the selected agent + status."""
    pred, params = scope_predicate(agent_sel)
    status_clause = "" if status == "all" else "AND status = %s"
    status_params = [] if status == "all" else [status]
    with conn() as c:
        row = c.execute(
            f"SELECT COUNT(*) AS n FROM improvement_proposals WHERE {pred} {status_clause}",
            (*params, *status_params),
        ).fetchone()
    return int(row["n"]) if row else 0


def _proposals_status_breakdown(agent_sel: str) -> dict[str, int]:
    """Count per status for the selected agent."""
    pred, params = scope_predicate(agent_sel)
    with conn() as c:
        rows = list(c.execute(
            f"SELECT status, COUNT(*) AS n FROM improvement_proposals "
            f"WHERE {pred} GROUP BY status ORDER BY n DESC",
            params,
        ))
    return {r["status"]: int(r["n"]) for r in rows}


_PROPOSAL_STATUS_ORDER = ["open", "accepted", "applied", "rejected", "superseded"]


def _proposals_table(agent_sel: str, status_filter: str, limit: int = 200) -> pd.DataFrame:
    """Fetch improvement_proposals scoped to agent + optional status filter."""
    pred, params = scope_predicate(agent_sel)
    status_clause = "" if status_filter == "all" else "AND status = %s"
    status_params = [] if status_filter == "all" else [status_filter]
    with conn() as c:
        rows = list(c.execute(
            f"""
            SELECT id, created_ts, category, confidence, title, rationale,
                   proposed_change, status
            FROM improvement_proposals
            WHERE {pred} {status_clause}
            ORDER BY
                CASE status
                    WHEN 'open'       THEN 1
                    WHEN 'accepted'   THEN 2
                    WHEN 'applied'    THEN 3
                    WHEN 'rejected'   THEN 4
                    WHEN 'superseded' THEN 5
                    ELSE 6
                END,
                confidence DESC NULLS LAST,
                created_ts DESC
            LIMIT %s
            """,
            (*params, *status_params, limit),
        ))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    now = pd.Timestamp.now(tz="UTC")
    df["age"] = (now - pd.to_datetime(df["created_ts"])).dt.total_seconds().apply(
        lambda s: (
            f"{int(s // 86400)}d" if s >= 86400
            else f"{int(s // 3600)}h" if s >= 3600
            else f"{int(s // 60)}m"
        )
    )
    df["created"] = df["created_ts"].apply(fmt_ist_short)
    return df[["id", "created", "age", "category", "confidence",
               "title", "rationale", "proposed_change", "status"]]


# ─── render ────────────────────────────────────────────────────────────

# The loop is per-agent (house improves code; freestyle agents tune persona /
# strategy_config). Scope every panel to the selected agent (default: house).
agent_sel, _agent_names = agent_selectbox(default=HOUSE_ID)
st.caption(f"Loop activity for **{agent_label(agent_sel, _agent_names)}**.")

paused = _autonomy_paused()
if paused:
    st.error("⏸️  Autonomy paused — Tester or human halted the loop. "
             "Check agent_runs for the cause.")

# ─── 0. Live experiments & economics (global; manage on the ⚙️ Settings page) ─
import os as _os  # noqa: E402  (display-only, local to render)

st.subheader("Live experiments & economics")
_flags = [
    ("F2 min-edge gate", True, "always on"),
    ("F5 conviction sizing", live_flag("CONVICTION_SIZING_ENABLED"), "⚠ review pending"),
    ("F7 context signals", live_flag("CONTEXT_SIGNALS_ENABLED"), "needs engine"),
    ("F6 per-strategy slots", live_flag("HOUSE_STRATEGY_KEYED_SLOTS"), "⚠ review pending"),
    ("Context engine", bool(_os.environ.get("CONTEXT_ENGINE_URL")), ".env"),
    ("Autonomy", not paused, "loop active" if not paused else "paused"),
]
fcols = st.columns(len(_flags))
for col, (name, on, note) in zip(fcols, _flags):
    col.metric(name, "ON" if on else "OFF")
    if note:
        col.caption(note)
st.caption("Toggle these on the ⚙️ Settings page → Feature flags. ⚠ flags should "
           "stay OFF until adversarially reviewed.")

try:
    _econ = book_economics(group_by="strategy")
    if _econ:
        _erows = [{
            "Strategy": k, "Trades": e.n, "Gross ₹": float(e.gross_pnl),
            "Cost ₹": float(e.charges), "Net ₹": float(e.net_pnl),
            "E2C": float(e.realised_e2c), "Win %": float(e.win_pct),
        } for k, e in sorted(_econ.items(), key=lambda kv: kv[1].net_pnl)]
        st.caption("Per-strategy unit economics — E2C (avg move ÷ cost) ≥ 3 is the "
                   "bar; below it, the ~₹13/trade cost eats the edge.")
        wrapped_table(pd.DataFrame(_erows))
except Exception as _e:  # never break the page on an analytics hiccup
    st.caption(f"economics unavailable: {_e}")

# ─── Instinct ledger (G4: durable, self-correcting learnings) ──────────
st.subheader("Instinct ledger — what it's learned")
st.caption("Verified lessons promoted into durable memory the agent now applies. "
           "Confidence DECAYS if the agent keeps losing after a lesson ships — a "
           "decayed instinct reopens its source cluster so the loop re-fixes it.")
try:
    from helm.agents.instincts import active_instincts as _active_instincts

    def _owns_instinct(cid: str) -> bool:
        if agent_sel in (HOUSE_ID, "house", "house-claude"):
            return cid in ("house", "house-claude", HOUSE_ID)
        return cid == agent_sel

    _ins = [r for r in _active_instincts() if _owns_instinct(r["competitor_id"])]
    if not _ins:
        st.caption("No instincts yet for this agent — they appear when one of its "
                   "proposal clusters ships and passes the eval-gate.")
    else:
        wrapped_table(pd.DataFrame([{
            "Lesson": (r["statement"] or "")[:90],
            "Encoded in": r["artifact_kind"] or r["layer"] or "—",
            "Confidence": f"{float(r['confidence']):.2f}",
            "Status": "active" if r["status"] == "active" else "⚠ decayed",
            "Hits": r["hits"], "Misses": r["misses"],
            "Promoted": fmt_ist_short(r["promoted_ts"]),
        } for r in _ins]), right_align=["Confidence", "Hits", "Misses"])
except Exception as _ie:  # never break the page on the ledger
    st.caption(f"instinct ledger unavailable: {_ie}")

latest = _latest_snapshot(agent_sel)
if not latest:
    st.warning(
        f"No metrics snapshots yet for {agent_label(agent_sel, _agent_names)}. "
        "The first one lands at 15:35 IST today; run "
        "`python scripts/snapshot_metrics.py` for an immediate sample."
    )

if latest:
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

    hist = _snapshot_history(agent_sel)
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
live_proposals_open = _proposals_live_count(agent_sel, "open")
qcol1, qcol2, qcol3 = st.columns(3)
qcol1.metric("Improvement proposals (open)", live_proposals_open)
if latest:
    qcol2.metric("Agent tasks (open/in-progress)", latest["tasks_open"])
    qcol3.metric("Releases unverified", latest["releases_unverified"])
else:
    qcol2.metric("Agent tasks (open/in-progress)", "—")
    qcol3.metric("Releases unverified", "—")

prop_df = _proposals_open_by_category(agent_sel)
if not prop_df.empty:
    st.caption("Open proposals by category")
    st.bar_chart(prop_df.set_index("category"))

# ─── 2b. Proposals detail table ───────────────────────────────────────
st.subheader("Proposals")
_status_opts = ["open", "accepted", "applied", "rejected", "superseded", "all"]
pcol1, pcol2 = st.columns([1, 3])
prop_status_filter = pcol1.selectbox(
    "Status", _status_opts, index=0, key="prop_status_filter"
)
breakdown = _proposals_status_breakdown(agent_sel)
breakdown_parts = [f"**{s}** {breakdown.get(s, 0)}" for s in _PROPOSAL_STATUS_ORDER]
pcol2.caption("Status breakdown: " + "  ·  ".join(breakdown_parts))

proposals_df = _proposals_table(agent_sel, prop_status_filter)
if proposals_df.empty:
    st.caption(
        f"No proposals with status '{prop_status_filter}' for "
        f"{agent_label(agent_sel, _agent_names)}."
    )
else:
    st.caption(
        f"{len(proposals_df)} proposal(s) shown "
        f"(status: {prop_status_filter}, agent: {agent_label(agent_sel, _agent_names)}). "
        "Hover a cell to read the full text."
    )
    wrapped_table(
        proposals_df,
        height=480,
        right_align={"id", "confidence"},
        clamp_cols={"rationale": 400, "proposed_change": 400},
    )

# ─── 3. Releases timeline ─────────────────────────────────────────────
st.subheader("Releases")
rel_df = _releases(agent_sel)
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
runs_df = _agent_runs(agent_sel)
if runs_df.empty:
    st.caption("No agent runs yet.")
else:
    runs_view = runs_df[["started_ts", "agent", "invocation", "outcome",
                         "latency_ms", "task_id", "release_id", "summary"]]
    st.dataframe(runs_view, use_container_width=True, hide_index=True)

# ─── 5. Open tasks table ──────────────────────────────────────────────
st.subheader("Open / in-progress tasks")
tasks_df = _agent_tasks(agent_sel)
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

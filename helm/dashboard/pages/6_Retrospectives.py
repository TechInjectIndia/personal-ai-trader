"""
Retrospectives page — plain-English reviews of every closed trade and SKIP.

Each row in `trade_retrospectives` answers four questions in everyday
language for a non-technical reader:

  1. Why the bot acted (entered or passed)
  2. What the price actually did
  3. Whether the decision was a good call, a bad call, lucky, or unlucky
  4. What can be learned for next time

This page lists the most recent reviews with a verdict badge, then offers
an expandable detail panel per item. A "Re-run" button at the top triggers
the retro cron in --force-window mode so new closes show up immediately.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from helm.dashboard.agents import HOUSE_ID, agent_label, agent_selectbox, scope_predicate
from helm.dashboard.format import IST, fmt_clock, fmt_ist, wrapped_table
from helm.dashboard.theme import apply_theme, page_header
from helm.data.store import conn

REPO_ROOT = Path(__file__).resolve().parents[3]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

st.set_page_config(page_title="Helm — Retrospectives", page_icon="🔁", layout="wide")
apply_theme()
page_header("Retrospectives", "What the bot learned from every call", icon="🔁")
st.caption(
    "After every closed trade and every skipped idea, the bot writes a plain-English "
    "review: why we acted, what happened, whether it was a good call, and what to "
    "remember for next time."
)


# ───────────────────────── verdict badge styling ─────────────────────────
BADGES = {
    "GOOD_CALL": ("✅ Good call", "#16a34a"),
    "UNLUCKY":   ("😕 Unlucky",   "#2563eb"),
    "LUCKY":     ("🍀 Lucky",     "#7c3aed"),
    "BAD_CALL":  ("❌ Bad call",  "#dc2626"),
    "MIXED":     ("🤷 Mixed",     "#6b7280"),
}


def badge(label: str) -> str:
    text, colour = BADGES.get(label, (label, "#374151"))
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 8px;'
        f'border-radius:6px;font-size:0.85em;">{text}</span>'
    )


# ───────────────────────── agent + date/kind/verdict filters ─────────────────────────
today = date.today()
agent_sel, _agent_names = agent_selectbox(default=HOUSE_ID, key="retros_agent")
st.caption(f"Showing retrospectives for **{agent_label(agent_sel, _agent_names)}**.")

fc1, fc2, fc3, fc4 = st.columns([1, 1, 1, 2])
date_from = fc1.date_input("From", today - timedelta(days=14))
date_to = fc2.date_input("To", today)
kind_filter = fc3.selectbox("Kind", ("All", "TRADE", "SKIP"))
verdict_filter = fc4.multiselect(
    "Verdict",
    options=list(BADGES.keys()),
    default=[],
    help="Empty = show all verdicts",
)

if date_from > date_to:
    st.error("`From` must be on or before `To`.")
    st.stop()


# ───────────────────────── fetch ─────────────────────────
@st.cache_data(ttl=30, show_spinner=False)
def fetch_retros(
    date_from: date, date_to: date,
    kind: str, verdicts: tuple[str, ...],
    agent: str,
) -> list[dict]:
    agent_pred, agent_params = scope_predicate(agent, alias="r")
    clauses = ["r.created_ts >= %s", "r.created_ts < %s", agent_pred]
    args: list = [
        datetime.combine(date_from, datetime.min.time(), tzinfo=IST),
        datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=IST),
        *agent_params,
    ]
    if kind != "All":
        clauses.append("r.kind = %s")
        args.append(kind)
    if verdicts:
        clauses.append("r.verdict_label = ANY(%s)")
        args.append(list(verdicts))
    sql = f"""
        SELECT r.*,
               t.symbol  AS t_symbol,  t.side AS t_side, t.qty AS t_qty,
               t.entry_price AS t_entry, t.exit_price AS t_exit,
               t.exit_reason AS t_exit_reason,
               t.pnl_inr AS t_pnl, t.net_pnl_inr AS t_net_pnl,
               t.entry_ts AS t_entry_ts, t.exit_ts AS t_exit_ts,
               s.symbol  AS s_symbol,  s.side AS s_side,
               s.entry_price AS s_entry, s.stop_loss AS s_stop, s.target AS s_target,
               s.strategy AS s_strategy, s.ts AS s_ts,
               d.actor AS d_actor, d.verdict AS d_verdict
        FROM trade_retrospectives r
        LEFT JOIN paper_trades t ON t.id = r.trade_id
        LEFT JOIN decisions    d ON d.id = r.decision_id
        LEFT JOIN signals      s ON s.id = d.signal_id
        WHERE {" AND ".join(clauses)}
        ORDER BY r.created_ts DESC
    """
    with conn() as c:
        return list(c.execute(sql, tuple(args)))


retros = fetch_retros(
    date_from, date_to, kind_filter, tuple(verdict_filter), agent_sel,
)


# ───────────────────────── manual re-run ─────────────────────────
with st.expander("Run the retro cron now", expanded=False):
    st.caption(
        "Same code path as the every-10-min cron, but bypasses the trading-window "
        "check. SKIP retros for today are only judged after square-off (15:15 IST) "
        "since the counterfactual replay needs the full day's price action."
    )
    rc1, rc2 = st.columns([1, 1])
    only_trade_id = rc1.text_input("Specific paper_trades.id (optional)", "")
    only_decision_id = rc2.text_input("Specific decisions.id for a SKIP (optional)", "")
    if st.button("Run retro now", type="primary"):
        cmd = [str(VENV_PYTHON), str(REPO_ROOT / "scripts" / "retro_trades.py"),
               "--force-window"]
        tid = only_trade_id.strip()
        did = only_decision_id.strip()
        if tid and did:
            st.error("Pick either a trade id or a decision id, not both.")
            st.stop()
        if tid:
            if not tid.isdigit():
                st.error(f"Trade id must be a positive integer, got {tid!r}.")
                st.stop()
            cmd += ["--trade-id", tid]
        if did:
            if not did.isdigit():
                st.error(f"Decision id must be a positive integer, got {did!r}.")
                st.stop()
            cmd += ["--decision-id", did]
        with st.spinner("Running retro… this can take 10-30s per item."):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(REPO_ROOT),
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                st.error("Retro timed out after 10 min.")
                st.stop()
        if proc.returncode == 0:
            st.success(f"Retro exited 0 · {fmt_clock()}")
        else:
            st.error(f"Retro exited {proc.returncode}")
        if proc.stdout.strip():
            st.code(proc.stdout, language="text")
        if proc.stderr.strip():
            st.code(proc.stderr, language="text")
        fetch_retros.clear()


# ───────────────────────── headline counts ─────────────────────────
if not retros:
    st.info("No retrospectives in this range yet.")
    st.stop()

st.divider()
counts: dict[str, int] = {}
for r in retros:
    counts[r["verdict_label"]] = counts.get(r["verdict_label"], 0) + 1
cols = st.columns(len(BADGES) + 1)
cols[0].metric("Total reviews", len(retros))
for i, label in enumerate(BADGES.keys()):
    text, _ = BADGES[label]
    cols[i + 1].metric(text, counts.get(label, 0))


# ───────────────────────── per-retro detail ─────────────────────────
st.subheader("Reviews")

for r in retros:
    is_trade = r["kind"] == "TRADE"
    sym = r["t_symbol"] if is_trade else r["s_symbol"]
    when = fmt_ist(r["created_ts"])
    if is_trade:
        net = float(r["t_net_pnl"]) if r["t_net_pnl"] is not None else float(r["t_pnl"] or 0)
        money = f"₹{net:+,.0f}"
    else:
        money = "passed"

    header = (
        f"**{sym}** · {r['kind']} · {money} · "
        f"{BADGES[r['verdict_label']][0]} · {when}"
    )

    with st.expander(header, expanded=False):
        st.markdown(badge(r["verdict_label"]), unsafe_allow_html=True)
        st.markdown(f"**Summary**  \n{r['summary_layman']}")

        # Plain-English narrative — the headline value of this page.
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Why we acted**")
            st.write(r["why_we_acted"])
            st.markdown("**What happened**")
            st.write(r["what_happened"])
        with c2:
            st.markdown("**Verdict reasoning**")
            st.write(r["verdict_reasoning"])
            st.markdown("**Learnings**")
            for ln in r["learnings"]:
                st.write(f"• {ln}")

        # Structured scores + tags — useful for later clustering / analytics.
        s1, s2, s3 = st.columns(3)
        s1.metric("Signal quality", f"{r['signal_quality_score']}/5")
        s2.metric("Decision quality", f"{r['decision_quality_score']}/5")
        s3.metric("Execution quality", f"{r['execution_quality_score']}/5")
        if r["tags"]:
            st.caption("Tags: " + " · ".join(f"`{t}`" for t in r["tags"]))

        # Underlying trade / signal facts — collapsed by default to keep the
        # narrative front and centre.
        with st.expander("Underlying trade / signal facts", expanded=False):
            if is_trade:
                facts = {
                    "Symbol": r["t_symbol"],
                    "Side": r["t_side"],
                    "Qty": r["t_qty"],
                    "Entry": float(r["t_entry"]),
                    "Exit": float(r["t_exit"]),
                    "Why exit": r["t_exit_reason"],
                    "Gross P&L (₹)": float(r["t_pnl"] or 0),
                    "Net P&L (₹)": (
                        float(r["t_net_pnl"]) if r["t_net_pnl"] is not None else None
                    ),
                    "Entered": fmt_ist(r["t_entry_ts"]),
                    "Exited": fmt_ist(r["t_exit_ts"]),
                    "Strategy": r["s_strategy"],
                    "AI verdict": r["d_verdict"],
                    "AI actor": r["d_actor"],
                }
            else:
                facts = {
                    "Symbol": r["s_symbol"],
                    "Proposed side": r["s_side"],
                    "Proposed entry": float(r["s_entry"]),
                    "Proposed stop": float(r["s_stop"]),
                    "Proposed target": (
                        float(r["s_target"]) if r["s_target"] is not None else None
                    ),
                    "Signal time": fmt_ist(r["s_ts"]),
                    "Strategy": r["s_strategy"],
                    "AI verdict": r["d_verdict"],
                    "AI actor": r["d_actor"],
                }
            df = pd.DataFrame(
                [{"Field": k, "Value": v} for k, v in facts.items()],
            )
            wrapped_table(df)

        st.caption(
            f"Model: `{r['model']}` · LLM mode: `{r['llm_mode']}` · "
            f"Retro id: {r['id']}"
        )

st.caption(f"Cached 30s · {fmt_clock()}")

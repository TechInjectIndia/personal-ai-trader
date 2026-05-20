"""
Competition League page — the multi-agent leaderboard + "how it thinks" view.

Five panels:
  1. Standings — equity-ranked leaderboard of every competitor (house + freestyle)
  2. Equity bar — equity per competitor at a glance
  3. This week's mandates — each freestyle agent's chosen universe + rationale
  4. How it thinks — recent agent_invocations for a selected competitor, with the
     parsed actions / commentary and the raw prompt+response
  5. Backend quotas — per-backend call usage + any active pause

Read-only over Postgres, like the rest of the dashboard. The standings reuse
helm.competition.leaderboard so the house book (competitor_id NULL OR
'house-claude') is counted correctly.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from helm.competition.leaderboard import leaderboard
from helm.competition.mandate import current_mandate, current_week_start
from helm.competition.quota import quota_status
from helm.data.store import conn
from helm.dashboard.format import fmt_ist

st.set_page_config(page_title="Helm — Competition League", page_icon="🏆", layout="wide")
st.title("Competition League")
st.caption("Five AI agents, five isolated ₹50k wallets, same market. "
           "Judge them on equity AND on how they reason.")


# ─── data helpers ──────────────────────────────────────────────────────

def _recent_invocations(competitor_id: str, limit: int = 25) -> list[dict]:
    with conn() as c:
        return list(c.execute(
            """
            SELECT id, ts, backend, model, ok, latency_ms, prompt, raw_output, error
            FROM agent_invocations
            WHERE competitor_id = %s
            ORDER BY ts DESC LIMIT %s
            """,
            (competitor_id, limit),
        ))


# ─── 1. Standings ──────────────────────────────────────────────────────
rows = leaderboard()
if not rows:
    st.warning("No competitors registered yet. Run `python scripts/migrate_competition.py` "
               "then `python scripts/seed_competitors.py`.")
    st.stop()

leader = rows[0]
c1, c2, c3, c4 = st.columns(4)
c1.metric("🥇 Leader", leader.name, delta=f"₹{float(leader.equity):,.0f} equity")
c2.metric("Leader P&L", f"₹{float(leader.realised_net_pnl):,.0f}",
          delta=f"{leader.progress_pct:+.1f}% vs start")
active_trades = sum(r.open_positions for r in rows)
c3.metric("Open positions (all)", active_trades)
total_trades = sum(r.trades for r in rows)
c4.metric("Trades booked (all)", total_trades)

st.subheader("Standings")
table = pd.DataFrame([{
    "#": r.rank,
    "competitor": r.name,
    "backend": r.backend,
    "type": r.autonomy_level,
    "status": r.status,
    "equity ₹": round(float(r.equity)),
    "P&L ₹": round(float(r.realised_net_pnl)),
    "prog %": round(r.progress_pct, 1),
    "open": r.open_positions,
    "trades": r.trades,
    "win %": round(r.win_rate_pct, 0) if r.win_rate_pct is not None else None,
    "available ₹": round(float(r.available)),
} for r in rows])
st.dataframe(table, use_container_width=True, hide_index=True)

# ─── 2. Equity bar ─────────────────────────────────────────────────────
st.subheader("Equity by competitor")
equity_df = pd.DataFrame(
    {"equity": [float(r.equity) for r in rows]},
    index=[r.name for r in rows],
)
st.bar_chart(equity_df, height=260)

# ─── 3. This week's mandates ───────────────────────────────────────────
st.subheader(f"Mandates — week of {current_week_start():%d %b %Y}")
freestyle = [r for r in rows if r.autonomy_level == "freestyle"]
any_mandate = False
for r in freestyle:
    m = current_mandate(r.competitor_id)
    if m and m.get("universe"):
        any_mandate = True
        with st.expander(f"{r.name} — {len(m['universe'])} symbols", expanded=False):
            st.write("**Universe:** " + ", ".join(str(s) for s in m["universe"]))
            if m.get("rationale"):
                st.write("**Rationale:** " + str(m["rationale"]))
            if m.get("strategy_config"):
                st.json(m["strategy_config"])
            st.caption(f"set {fmt_ist(m.get('created_at'))}")
if not any_mandate:
    st.info("No mandates set yet for this week. "
            "Run `python scripts/plan_mandates.py` to have each agent pick its universe.")

# ─── 4. How it thinks ──────────────────────────────────────────────────
st.subheader("How it thinks")
names = {r.competitor_id: r.name for r in rows}
pick = st.selectbox("Competitor", options=list(names),
                    format_func=lambda cid: names.get(cid, cid))
invs = _recent_invocations(pick)
if not invs:
    st.caption("No backend calls recorded yet for this competitor. "
               "Calls land here once it runs (scripts/run_competitors.py) or "
               "plans a mandate (scripts/plan_mandates.py).")
else:
    for inv in invs:
        status = "✅" if inv["ok"] else "⚠️"
        latency = f"{(inv['latency_ms'] or 0) / 1000:.1f}s"
        header = f"{status} {fmt_ist(inv['ts'])} · {inv['backend']} · {latency}"
        with st.expander(header, expanded=False):
            if inv["error"]:
                st.error(inv["error"])
            parsed = None
            if inv["raw_output"]:
                try:
                    parsed = json.loads(inv["raw_output"])
                except (json.JSONDecodeError, TypeError):
                    parsed = None
            if isinstance(parsed, dict) and parsed.get("actions"):
                st.write("**Actions:**")
                st.dataframe(pd.DataFrame(parsed["actions"]),
                             use_container_width=True, hide_index=True)
                if parsed.get("commentary"):
                    st.caption("💬 " + str(parsed["commentary"]))
            elif isinstance(parsed, dict) and parsed.get("universe"):
                st.write("**Mandate universe:** " + ", ".join(
                    str(s) for s in parsed["universe"]))
                if parsed.get("rationale"):
                    st.caption(str(parsed["rationale"]))
            elif parsed is not None:
                st.json(parsed)
            with st.expander("raw prompt + response", expanded=False):
                st.text((inv["prompt"] or "")[:6000])
                st.divider()
                st.text((inv["raw_output"] or "")[:6000])

# ─── 5. Backend quotas ─────────────────────────────────────────────────
st.subheader("Backend quotas")
quotas = quota_status()
if not quotas:
    st.caption("No backend calls recorded yet — quotas populate on first use.")
else:
    qdf = pd.DataFrame([{
        "backend": q["backend"],
        "used / max": f"{q['calls_used']} / {q['max_calls']}",
        "window (min)": q["window_minutes"],
        "paused": "⏸️ until " + fmt_ist(q["paused_until"]) if q["paused"] else "—",
    } for q in quotas])
    st.dataframe(qdf, use_container_width=True, hide_index=True)

with st.expander("How this page is wired"):
    st.markdown(
        "- **Standings** come from `helm.competition.leaderboard` — the house "
        "book is `competitor_id NULL OR 'house-claude'`, everyone else is their "
        "own id, all on a ₹50k basis.\n"
        "- **Mandates** are weekly per-agent universes from `competitor_mandates` "
        "(`scripts/plan_mandates.py`); the dynamic poller "
        "(`scripts/poll_competition.py`) fetches candles for the extra symbols.\n"
        "- **How it thinks** is every backend call traced to `agent_invocations` "
        "by the quota-gated `helm.competition.backend.call_backend`.\n"
        "- **Quotas** are per-backend rolling windows in `backend_quota_state`; "
        "an exhausted backend auto-pauses until the window rolls.\n"
        "- The league runner (`scripts/run_competitors.py`) is **not yet on cron** "
        "— going live is a human decision.")

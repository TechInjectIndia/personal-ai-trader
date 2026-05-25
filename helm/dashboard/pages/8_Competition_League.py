"""
Competition League page — the multi-agent leaderboard + "how it thinks" view.

Panels:
  1. Standings — equity-ranked leaderboard of every competitor (house + freestyle)
  2. Agents — one avatar card per competitor: status, live-execution indicator,
     and its autonomous build team (PM → Dev/Engineer → Tester) with each
     member's last-run status. A green halo + pulsing dot = a run in flight now.
  3. Equity bar — equity per competitor at a glance
  4. This week's mandates — each freestyle agent's chosen universe + rationale
  5. How it thinks — recent agent_invocations for a selected competitor (paginated)
  6. Backend quotas — per-backend call usage + any active pause

Read-only over Postgres, like the rest of the dashboard. The standings reuse
helm.competition.leaderboard so the house book (competitor_id NULL OR
'house-claude') is counted correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from helm.competition.leaderboard import leaderboard
from helm.competition.mandate import current_mandate, current_week_start
from helm.competition.quota import quota_status
from helm.dashboard.agents import HOUSE_ID
from helm.dashboard.format import fmt_ist, paginate, wrapped_table
from helm.dashboard.theme import (
    agent_avatar,
    agent_card,
    agent_grid,
    apply_theme,
    kpi_card,
    kpi_grid,
    page_header,
    pill,
    team_chip,
)
from helm.data.store import conn

st.set_page_config(page_title="Helm — Competition League", page_icon="🏆", layout="wide")
apply_theme()
page_header("Competition League",
            "Five AI agents · ₹50k each · one human judge", icon="🏆")
st.caption("Five AI agents, five isolated ₹50k wallets, same market. "
           "Judge them on equity AND on how they reason.")


# ─── data helpers ──────────────────────────────────────────────────────

# Each competitor runs the same three-role build loop. agent_runs.agent stores
# the role key; we relabel 'engineer' → 'Dev' for the UI.
TEAM_ROLES = [("pm", "PM"), ("engineer", "Dev"), ("tester", "Tester")]
# A run that started within this many seconds (and isn't finished) still reads
# as "executing now" — most runs finish sub-second, so a recency window catches
# activity the bare finished_ts IS NULL check would miss.
RUNNING_WINDOW_S = 120


def _age(ts) -> str:
    """Compact age of a timestamp ("3m", "2h", "5d"). DB ts are tz-aware UTC."""
    if ts is None:
        return "—"
    s = (datetime.now(timezone.utc) - ts).total_seconds()
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


def _personas() -> dict[str, str]:
    with conn() as c:
        return {r["id"]: (r["persona"] or "")
                for r in c.execute("SELECT id, persona FROM competitors")}


def _team_activity() -> dict[str, dict[str, dict]]:
    """Latest run per (competitor, role). House runs carry competitor_id NULL or
    'house-claude', so both fold into the house id.

    The ``cid`` is computed in a subquery first so DISTINCT ON and ORDER BY
    reference the same plain column — repeating ``COALESCE(..., %s)`` in both
    would bind different parameter numbers and Postgres rejects the mismatch.
    """
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT DISTINCT ON (cid, agent)
                   cid, agent, started_ts, finished_ts, outcome, summary
            FROM (
                SELECT COALESCE(competitor_id, %s) AS cid, agent,
                       started_ts, finished_ts, outcome, summary
                FROM agent_runs
                WHERE agent IN ('pm', 'engineer', 'tester')
            ) t
            ORDER BY cid, agent, started_ts DESC
            """,
            (HOUSE_ID,),
        ))
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["cid"], {})[r["agent"]] = r
    return out


def _queue_counts() -> dict[str, dict[str, int]]:
    """Per-competitor open/in-flight task counts + unverified release counts."""
    out: dict[str, dict[str, int]] = {}
    with conn() as c:
        for r in c.execute(
            """
            SELECT cid,
                   COUNT(*) FILTER (WHERE status = 'open')        AS tasks_open,
                   COUNT(*) FILTER (WHERE status = 'in_progress') AS tasks_inflight
            FROM (SELECT COALESCE(competitor_id, %s) AS cid, status
                  FROM agent_tasks) t
            GROUP BY cid
            """, (HOUSE_ID,),
        ):
            out.setdefault(r["cid"], {}).update(
                tasks_open=int(r["tasks_open"]),
                tasks_inflight=int(r["tasks_inflight"]),
            )
        for r in c.execute(
            """
            SELECT cid, COUNT(*) FILTER (WHERE status = 'deployed') AS rel_unverified
            FROM (SELECT COALESCE(competitor_id, %s) AS cid, status
                  FROM releases) t
            GROUP BY cid
            """, (HOUSE_ID,),
        ):
            out.setdefault(r["cid"], {}).update(rel_unverified=int(r["rel_unverified"]))
    return out


def _recent_invocations(competitor_id: str, limit: int = 60) -> list[dict]:
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


def _build_card(row, persona: str, team: dict[str, dict],
                q: dict[str, int]) -> tuple[str, bool]:
    """Compose one roster card; also report whether the agent is executing now."""
    now = datetime.now(timezone.utc)
    inflight = q.get("tasks_inflight", 0)
    open_tasks = q.get("tasks_open", 0)
    unver = q.get("rel_unverified", 0)
    live = inflight > 0

    chips: list[str] = []
    for role_key, role_label in TEAM_ROLES:
        run = team.get(role_key)
        running = False
        if run is not None:
            running = (
                run["finished_ts"] is None
                or (now - run["started_ts"]).total_seconds() <= RUNNING_WINDOW_S
            )
        if role_key == "engineer" and inflight > 0:
            running = True
        if running:
            live = True

        if run is None:
            chips.append(team_chip(role_label, "none", "—", title="No runs recorded yet"))
        elif running:
            chips.append(team_chip(role_label, "run", "running",
                                   title=run.get("summary") or "in progress"))
        else:
            outcome = run.get("outcome") or "noop"
            kind = outcome if outcome in ("ok", "error", "noop") else "noop"
            chips.append(team_chip(role_label, kind, _age(run["started_ts"]),
                                   title=run.get("summary") or outcome))

    team_html = ('<div class="helm-team-label">Team · PM / Dev / Tester</div>'
                 '<div class="helm-team">' + "".join(chips) + "</div>")

    status_kind = {"active": "info", "paused": "warn",
                   "retired": "closed"}.get(row.status, "info")
    pills = pill(row.status or "—", status_kind)
    if live:
        pills += pill("executing", "live")

    foot_left = f"<b>₹{float(row.equity):,.0f}</b> · {row.progress_pct:+.1f}%"
    qbits = []
    if open_tasks:
        qbits.append(f"{open_tasks} open")
    if inflight:
        qbits.append(f"{inflight} building")
    if unver:
        qbits.append(f"{unver} unverified")
    foot_right = " · ".join(qbits) if qbits else "queue clear"

    card = agent_card(
        name=row.name, backend=row.backend, persona=persona,
        pills_html=pills, team_html=team_html,
        footer_left=foot_left, footer_right=foot_right, live=live,
    )
    return card, live


# ─── 1. Standings ──────────────────────────────────────────────────────
rows = leaderboard()
if not rows:
    st.warning("No competitors registered yet. Run `python scripts/migrate_competition.py` "
               "then `python scripts/seed_competitors.py`.")
    st.stop()

leader = rows[0]
active_trades = sum(r.open_positions for r in rows)
total_trades = sum(r.trades for r in rows)
_lead_pnl = float(leader.realised_net_pnl)
kpi_grid([
    kpi_card("Leader", leader.name, icon="medal",
             sub=f"₹{float(leader.equity):,.0f} equity", sub_kind="info"),
    kpi_card("Leader P&L", f"₹{_lead_pnl:,.0f}", icon="wallet",
             sub=f"{leader.progress_pct:+.1f}% vs start",
             sub_kind="pos" if _lead_pnl >= 0 else "neg"),
    kpi_card("Open positions (all)", f"{active_trades}", icon="folder"),
    kpi_card("Trades booked (all)", f"{total_trades}", icon="repeat"),
])

# ─── 2. Agents (avatars · status · build team · live execution) ─────────
st.subheader("Agents")
st.caption("Each competitor and its autonomous build team — PM proposes · Dev "
           "(Engineer) ships · Tester verifies. A green halo and a pulsing dot "
           "mean a run is in flight right now.")

_team = _team_activity()
_queues = _queue_counts()
_personas_map = _personas()
_backends = {r.competitor_id: r.backend for r in rows}
_names = {r.competitor_id: r.name for r in rows}

cards: list[str] = []
live_count = 0
for r in rows:
    card, is_live = _build_card(
        r, _personas_map.get(r.competitor_id, ""),
        _team.get(r.competitor_id, {}), _queues.get(r.competitor_id, {}),
    )
    cards.append(card)
    live_count += int(is_live)

if live_count:
    st.markdown(
        pill(f"{live_count} agent team(s) executing now", "live"),
        unsafe_allow_html=True,
    )
else:
    st.markdown(pill("All build loops idle", "closed"), unsafe_allow_html=True)

agent_grid(cards, cols=3)

st.subheader("Standings")
table = pd.DataFrame([{
    "#": r.rank,
    "Competitor": r.name,
    "Backend": r.backend,
    "Type": r.autonomy_level,
    "Status": r.status,
    "Equity ₹": f"{float(r.equity):,.0f}",
    "P&L ₹": f"{float(r.realised_net_pnl):,.0f}",
    "Prog %": f"{r.progress_pct:+.1f}",
    "Open": r.open_positions,
    "Trades": r.trades,
    "Win %": f"{r.win_rate_pct:.0f}" if r.win_rate_pct is not None else "—",
    "Available ₹": f"{float(r.available):,.0f}",
} for r in rows])
wrapped_table(table, right_align=["Equity ₹", "P&L ₹", "Prog %", "Open",
                                  "Trades", "Win %", "Available ₹"])

# ─── 3. Equity bar ─────────────────────────────────────────────────────
st.subheader("Equity by competitor")
equity_df = pd.DataFrame(
    {"equity": [float(r.equity) for r in rows]},
    index=[r.name for r in rows],
)
st.bar_chart(equity_df, height=260)

# ─── 4. This week's mandates ───────────────────────────────────────────
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

# ─── 5. How it thinks ──────────────────────────────────────────────────
st.subheader("How it thinks")
pick = st.selectbox("Competitor", options=list(_names),
                    format_func=lambda cid: _names.get(cid, cid))
st.markdown(
    '<div style="display:flex;align-items:center;gap:10px;margin:2px 0 6px;">'
    f'{agent_avatar(_names[pick], _backends.get(pick, ""), size=30)}'
    f'<span style="font-weight:600;color:#E6EDF3;">{_names[pick]}</span></div>',
    unsafe_allow_html=True,
)
invs = _recent_invocations(pick)
if not invs:
    st.caption("No backend calls recorded yet for this competitor. "
               "Calls land here once it runs (scripts/run_competitors.py) or "
               "plans a mandate (scripts/plan_mandates.py).")
else:
    page_invs = paginate(invs, key="league_invs", per_page=8, label="calls")
    for inv in page_invs:
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

# ─── 6. Backend quotas ─────────────────────────────────────────────────
st.subheader("Backend quotas")
quotas = quota_status()
if not quotas:
    st.caption("No backend calls recorded yet — quotas populate on first use.")
else:
    qdf = pd.DataFrame([{
        "Backend": q["backend"],
        "Used / max": f"{q['calls_used']} / {q['max_calls']}",
        "Window (min)": q["window_minutes"],
        "Paused": "⏸️ until " + fmt_ist(q["paused_until"]) if q["paused"] else "—",
    } for q in quotas])
    wrapped_table(qdf, right_align=["Window (min)"])

with st.expander("How this page is wired"):
    st.markdown(
        "- **Standings** come from `helm.competition.leaderboard` — the house "
        "book is `competitor_id NULL OR 'house-claude'`, everyone else is their "
        "own id, all on a ₹50k basis.\n"
        "- **Agents** join `competitors` with the latest `agent_runs` per role "
        "(PM/Engineer/Tester) plus open/in-flight `agent_tasks` and unverified "
        "`releases`; 'executing now' = a run with no `finished_ts`, a run within "
        f"the last {RUNNING_WINDOW_S}s, or a task `in_progress`.\n"
        "- **Mandates** are weekly per-agent universes from `competitor_mandates` "
        "(`scripts/plan_mandates.py`); the dynamic poller "
        "(`scripts/poll_competition.py`) fetches candles for the extra symbols.\n"
        "- **How it thinks** is every backend call traced to `agent_invocations` "
        "by the quota-gated `helm.competition.backend.call_backend`.\n"
        "- **Quotas** are per-backend rolling windows in `backend_quota_state`; "
        "an exhausted backend auto-pauses until the window rolls.\n"
        "- The league runner (`scripts/run_competitors.py`) is **not yet on cron** "
        "— going live is a human decision.")

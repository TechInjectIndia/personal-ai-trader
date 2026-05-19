"""Activity Log — live per-helper execution stream.

One section per helper (Price watcher / Idea spotter / Decision-maker /
Position minder), each showing the most recent audit rows with parsed
inputs and outputs. Auto-refreshes every few seconds so it feels live.

All data comes from the `audit` table — every cron run writes a row, so
this page is essentially a live tail of that table grouped by `actor`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from helm.dashboard.format import wrapped_table
from helm.data.store import conn

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="Helm — Activity Log", page_icon="📡", layout="wide")
st.title("Activity Log — what each helper is doing")

# ───────────────────────── controls ─────────────────────────
top_l, top_r = st.columns([3, 1])
with top_l:
    rows_per_helper = st.slider(
        "Rows per helper",
        min_value=10, max_value=200, value=40, step=10,
        help="How many recent audit rows to show for each helper.",
    )
with top_r:
    refresh_s = st.selectbox(
        "Auto-refresh",
        options=[("Off", 0), ("3s", 3), ("5s", 5), ("10s", 10), ("30s", 30)],
        format_func=lambda x: x[0],
        index=2,
    )[1]

if refresh_s:
    st_autorefresh(interval=refresh_s * 1000, key="activity_log_refresh")

now = datetime.now(IST)
st.caption(f"Last loaded {now.strftime('%H:%M:%S')} IST · "
           f"{'auto-refreshing every ' + str(refresh_s) + 's' if refresh_s else 'manual refresh only'}")

# ───────────────────────── data ─────────────────────────
HELPERS = [
    ("poll_market", "Price watcher", "Pulls live prices and rolls them into 1-minute candles."),
    ("scan_signals", "Idea spotter", "Runs each strategy against the latest candles; emits signals."),
    ("decide_signals", "Decision-maker (with AI)", "Sends each unconsumed signal to the LLM for a TAKE/SKIP verdict."),
    ("manage_positions", "Position minder", "Watches every open paper trade for stop/target/EOD."),
]


@st.cache_data(ttl=2, show_spinner=False)
def fetch_audit_for(actor: str, limit: int) -> list[dict]:
    with conn() as c:
        return list(c.execute(
            "SELECT ts, actor, event, detail FROM audit WHERE actor = %s "
            "ORDER BY ts DESC LIMIT %s",
            (actor, limit),
        ))


@st.cache_data(ttl=2, show_spinner=False)
def fetch_actor_counts() -> dict[str, dict]:
    """Today's per-helper counts + last-seen timestamp."""
    today_filter = (
        "ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') "
        "AT TIME ZONE 'Asia/Kolkata'"
    )
    with conn() as c:
        rows = list(c.execute(
            f"SELECT actor, COUNT(*) AS n, MAX(ts) AS last_ts "
            f"FROM audit WHERE {today_filter} GROUP BY actor"
        ))
    return {r["actor"]: {"n": r["n"], "last_ts": r["last_ts"]} for r in rows}


# ───────────────────────── per-helper summary header ─────────────────────────
counts = fetch_actor_counts()
st.subheader("Today at a glance")
cols = st.columns(len(HELPERS))
for col, (actor, label, _) in zip(cols, HELPERS):
    info = counts.get(actor, {})
    n = info.get("n", 0)
    last = info.get("last_ts")
    age_s = int((now - last.astimezone(IST)).total_seconds()) if last else None
    age_str = f"{age_s}s ago" if age_s is not None and age_s < 120 else (
        f"{age_s // 60}m ago" if age_s is not None else "never"
    )
    col.metric(label, f"{n} runs", age_str)

st.divider()


# ───────────────────────── helpers for rendering ─────────────────────────
def _fmt_ts(ts) -> str:
    return ts.astimezone(IST).strftime("%H:%M:%S")


def _summary_for_event(actor: str, event: str, detail: dict | None) -> str:
    """Render a one-line plain-English summary of an audit row."""
    d = detail or {}
    if actor == "poll_market" and event == "tick_poll":
        ins = d.get("inserted", 0)
        failed = d.get("failed", []) or []
        upserts = d.get("candle_upserts", 0)
        if failed:
            return f"polled · {ins} ok · {len(failed)} failed · candles {upserts}"
        return f"polled · {ins}/5 ok · candles upserts {upserts}"
    if actor == "scan_signals" and event == "scan_complete":
        em = d.get("emitted", 0)
        dup = d.get("skipped_dup", 0)
        checks = d.get("checks") or []
        n_checked = len(checks) or len(d.get("strategies", [])) * 5
        msg = f"scanned {n_checked} (strategy × symbol) · emitted {em}"
        if dup:
            msg += f" · dup-skipped {dup}"
        fired = d.get("fired") or []
        if fired:
            who = ", ".join(f"{f.get('strategy','?')}→{f.get('symbol','?')}" for f in fired)
            msg += f" · FIRED: {who}"
        return msg
    if actor == "decide_signals" and event == "decision":
        return (
            f"signal {d.get('signal_id','?')} → {d.get('verdict','?')} "
            f"(conf {d.get('confidence', 0):.2f})"
        )
    if actor == "decide_signals" and event == "run_summary":
        return (
            f"considered {d.get('considered',0)} · taken {d.get('taken',0)} "
            f"· skipped {d.get('skipped',0)} · errored {d.get('errored',0)} "
            f"· mode {d.get('mode','?')}"
        )
    if actor == "decide_signals" and event in ("api_error", "llm_error"):
        return f"ERROR: {(d.get('error') or '')[:120]}"
    if actor == "manage_positions" and event == "position_check":
        watched = d.get("watched", 0)
        closed = d.get("closed", 0)
        eod = d.get("eod", False)
        msg = f"watched {watched} · closed {closed}"
        if eod:
            msg += " · EOD"
        return msg
    if actor == "manage_positions" and event == "closed":
        return (
            f"closed trade {d.get('trade_id','?')} {d.get('symbol','?')} "
            f"{d.get('side','?')} → {d.get('reason','?')} · pnl ₹{d.get('pnl_inr','?')}"
        )
    if actor == "kite_auto_login":
        if event == "refreshed":
            return "Kite token refreshed"
        return f"{event}: {(d.get('error') or '')[:120]}"
    return event  # fallback


def _render_helper(actor: str, label: str, blurb: str, limit: int) -> None:
    rows = fetch_audit_for(actor, limit)
    with st.expander(f"**{label}**  ·  `{actor}`  ·  {len(rows)} most recent",
                     expanded=True):
        st.caption(blurb)
        if not rows:
            st.info("No audit rows yet for this helper.")
            return

        # Table view: timestamp + summary, with a row-pick to inspect detail.
        table = pd.DataFrame([
            {
                "Time (IST)": _fmt_ts(r["ts"]),
                "Event": r["event"],
                "Summary": _summary_for_event(r["actor"], r["event"], r["detail"]),
            }
            for r in rows
        ])
        wrapped_table(table, height=320)

        # Drilldown: pick a row to see full input/output JSON.
        idx_options = [
            f"{_fmt_ts(r['ts'])} · {r['event']}" for r in rows
        ]
        sel = st.selectbox(
            "Inspect a single run (full input/output)",
            options=range(len(idx_options)),
            format_func=lambda i: idx_options[i],
            key=f"sel_{actor}",
        )
        chosen = rows[sel]
        st.code(_pretty_detail(chosen["detail"]), language="json")


def _pretty_detail(detail: dict | None) -> str:
    import json
    if detail is None:
        return "(no detail)"
    return json.dumps(detail, indent=2, default=str)


# ───────────────────────── render each helper ─────────────────────────
for actor, label, blurb in HELPERS:
    _render_helper(actor, label, blurb, rows_per_helper)

# ───────────────────────── kite login (bonus row) ─────────────────────────
with st.expander("**Token watchdog**  ·  `kite_auto_login`  ·  daily login state",
                 expanded=False):
    rows = fetch_audit_for("kite_auto_login", 20)
    if not rows:
        st.info("No login attempts logged yet.")
    else:
        table = pd.DataFrame([
            {"Time (IST)": _fmt_ts(r["ts"]),
             "Event": r["event"],
             "Summary": _summary_for_event(r["actor"], r["event"], r["detail"])}
            for r in rows
        ])
        wrapped_table(table)

"""Helm dashboard — intraday bot view (paper trading)."""

import os
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from helm.config import WATCHLIST, TRADING_END, TRADING_START, SQUARE_OFF_AT, live_risk_limits
from helm.wallet import wallet_state
from helm.dashboard.format import IST, fmt_clock, fmt_ist, fmt_window, wrapped_table
from helm.data.store import conn

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

st.set_page_config(page_title="Helm — Intraday", page_icon="🧭", layout="wide")


def _try_auto_relogin() -> bool:
    """Trigger kite_auto_login.py; return True if it exits 0."""
    try:
        r = subprocess.run(
            [str(VENV_PYTHON), str(REPO_ROOT / "scripts" / "kite_auto_login.py")],
            check=False,
            capture_output=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@st.cache_data(ttl=20, show_spinner=False)
def fetch_account():
    load_dotenv(ENV_PATH, override=True)
    api_key = os.environ.get("KITE_API_KEY")
    token = os.environ.get("KITE_ACCESS_TOKEN")
    if not api_key or not token:
        return {"error": "missing-creds"}

    def _call(tok: str):
        k = KiteConnect(api_key=api_key)
        k.set_access_token(tok)
        return {"profile": k.profile(), "margins": k.margins(segment="equity")}

    try:
        return _call(token)
    except TokenException:
        # Token may have been killed by a concurrent Kite web/mobile login.
        # Self-heal by re-running auto-login once, then retry.
        if not _try_auto_relogin():
            return {"error": "token-expired", "detail": "auto-relogin failed"}
        load_dotenv(ENV_PATH, override=True)
        new_token = os.environ.get("KITE_ACCESS_TOKEN")
        if not new_token or new_token == token:
            return {"error": "token-expired", "detail": "auto-relogin produced no new token"}
        try:
            return _call(new_token)
        except TokenException as exc:
            return {"error": "token-expired", "detail": str(exc)}
        except Exception as exc:
            return {"error": "api-error", "detail": str(exc)}
    except Exception as exc:
        return {"error": "api-error", "detail": str(exc)}


@st.cache_data(ttl=30, show_spinner=False)
def fetch_token_health():
    """Last `kite_auto_login` audit row — surfaces silent cron failures."""
    with conn() as c:
        row = c.execute(
            "SELECT ts, event, detail FROM audit "
            "WHERE actor = 'kite_auto_login' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return {"status": "never"}
    age_h = (datetime.now(IST) - row["ts"].astimezone(IST)).total_seconds() / 3600
    return {"status": row["event"], "ts": row["ts"], "age_h": age_h, "detail": row["detail"]}


@st.cache_data(ttl=15, show_spinner=False)
def fetch_bot_state():
    today_filter = """
        ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
    """
    today_ist = (
        "date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'"
    )
    with conn() as c:
        latest_ticks = list(c.execute(
            """
            SELECT DISTINCT ON (symbol) symbol, ts, ltp
            FROM ticks
            ORDER BY symbol, ts DESC
            """
        ))
        candle_counts = {
            r["symbol"]: r["n"]
            for r in c.execute(
                f"SELECT symbol, COUNT(*) AS n FROM candles_1m WHERE bar_ts >= "
                f"{today_ist} "
                f"GROUP BY symbol"
            )
        }
        signals_today = list(c.execute(
            f"SELECT id, ts, strategy, symbol, side, entry_price, stop_loss, target, consumed, rationale "
            f"FROM signals WHERE {today_filter} ORDER BY ts DESC"
        ))
        open_trades = list(c.execute(
            "SELECT id, symbol, side, qty, entry_price, entry_ts, stop_loss, target "
            "FROM paper_trades WHERE status = 'OPEN' ORDER BY entry_ts DESC"
        ))
        closed_today = list(c.execute(
            f"SELECT symbol, side, qty, entry_price, exit_price, exit_reason, "
            f"pnl_inr, charges_inr, net_pnl_inr, entry_ts, exit_ts "
            f"FROM paper_trades WHERE status = 'CLOSED' AND exit_ts >= {today_ist} "
            f"ORDER BY exit_ts DESC"
        ))
        # Today's aggregates — purchases/sellings split by which leg of which
        # side is a buy vs a sell. A BUY trade's entry is a purchase, its exit
        # is a sell; a SELL (short) trade is the mirror. Open trades only
        # contribute their entry leg (no exit yet). Legacy rows without
        # charges_inr fall back to gross P&L via COALESCE so totals stay
        # self-consistent.
        today_totals = c.execute(
            f"""
            SELECT
                COALESCE(SUM(qty * entry_price) FILTER (WHERE status='OPEN'), 0)
                    AS open_investment,
                COALESCE(SUM(
                    CASE WHEN side='BUY'  THEN qty * entry_price ELSE 0 END
                ) FILTER (WHERE entry_ts >= {today_ist}), 0)
                  + COALESCE(SUM(
                    CASE WHEN side='SELL' THEN qty * exit_price  ELSE 0 END
                ) FILTER (WHERE status='CLOSED' AND exit_ts >= {today_ist}), 0)
                    AS purchases_today,
                COALESCE(SUM(
                    CASE WHEN side='SELL' THEN qty * entry_price ELSE 0 END
                ) FILTER (WHERE entry_ts >= {today_ist}), 0)
                  + COALESCE(SUM(
                    CASE WHEN side='BUY'  THEN qty * exit_price  ELSE 0 END
                ) FILTER (WHERE status='CLOSED' AND exit_ts >= {today_ist}), 0)
                    AS sellings_today,
                COALESCE(SUM(pnl_inr)
                         FILTER (WHERE status='CLOSED' AND exit_ts >= {today_ist}), 0)
                    AS gross_pnl_today,
                COALESCE(SUM(COALESCE(charges_inr, 0))
                         FILTER (WHERE status='CLOSED' AND exit_ts >= {today_ist}), 0)
                    AS charges_today,
                COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr))
                         FILTER (WHERE status='CLOSED' AND exit_ts >= {today_ist}), 0)
                    AS net_pnl_today
            FROM paper_trades
            """
        ).fetchone()
        lifetime_totals = c.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status='CLOSED')                          AS closed_trades,
                COALESCE(SUM(CASE WHEN side='BUY'  THEN qty * entry_price ELSE 0 END), 0)
                  + COALESCE(SUM(CASE WHEN side='SELL' THEN qty * exit_price ELSE 0 END)
                             FILTER (WHERE status='CLOSED'), 0)                  AS purchases,
                COALESCE(SUM(CASE WHEN side='SELL' THEN qty * entry_price ELSE 0 END), 0)
                  + COALESCE(SUM(CASE WHEN side='BUY'  THEN qty * exit_price ELSE 0 END)
                             FILTER (WHERE status='CLOSED'), 0)                  AS sellings,
                COALESCE(SUM(COALESCE(charges_inr, 0))
                         FILTER (WHERE status='CLOSED'), 0)                      AS charges,
                COALESCE(SUM(pnl_inr) FILTER (WHERE status='CLOSED'), 0)         AS gross_pnl,
                COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr))
                         FILTER (WHERE status='CLOSED'), 0)                      AS net_pnl
            FROM paper_trades
            """
        ).fetchone()
        recent_audit = list(c.execute(
            "SELECT ts, actor, event, detail FROM audit ORDER BY ts DESC LIMIT 25"
        ))
    return {
        "latest_ticks": latest_ticks,
        "candle_counts": candle_counts,
        "signals_today": signals_today,
        "open_trades": open_trades,
        "closed_today": closed_today,
        "today_totals": today_totals,
        "lifetime_totals": lifetime_totals,
        "recent_audit": recent_audit,
    }


# ───────────────────────── header ─────────────────────────
st.title("Helm — intraday bot")
st.caption(f"Watchlist: {' · '.join(WATCHLIST)} · paper mode")

acct = fetch_account()
if "error" in acct:
    if acct["error"] == "missing-creds":
        st.warning("Zerodha credentials not set in `.env`.")
    elif acct["error"] == "token-expired":
        st.warning(
            f"Zerodha token expired — auto-login cron runs daily at 06:10 IST; "
            f"check logs/kite_login.log or run `python scripts/kite_auto_login.py` manually. "
            f"({acct['detail']})"
        )
    else:
        st.error(f"Zerodha error: {acct['detail']}")
else:
    p = acct["profile"]
    cash = float(acct["margins"].get("available", {}).get("cash", 0) or 0)
    st.success(f"Broker: {p['user_name']} ({p['user_id']}) · live cash ₹{cash:,.2f}")

# Kite auto-login watchdog — flags silent cron failures.
health = fetch_token_health()
if health["status"] == "never":
    st.warning(
        "Kite auto-login has never run. Confirm the cron entry "
        "(`crontab -l | grep kite_auto_login`) and that .env has "
        "KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET set."
    )
elif health["status"] == "failed":
    err = (health["detail"] or {}).get("error", "unknown")
    st.error(
        f"Kite auto-login last attempt FAILED at {fmt_ist(health['ts'])} — {err}. "
        f"Check logs/kite_login.log; run `python scripts/kite_auto_login.py` to retry."
    )
elif health["status"] == "refreshed" and health["age_h"] > 25:
    st.warning(
        f"Kite token refresh is stale — last success {health['age_h']:.0f}h ago "
        f"({fmt_ist(health['ts'])}). The 06:10 IST cron may have failed silently."
    )

state = fetch_bot_state()
now_ist = datetime.now(IST)
risk_limits = live_risk_limits()
wallet = wallet_state()
today = state["today_totals"]
lifetime = state["lifetime_totals"]

# ───────────────────────── wallet card ─────────────────────────
# The bot trades from one cash pool. This card is the headline: where the
# pool is now, how much is free to deploy, and how close we are to the goal.
goal_hit = wallet.equity >= wallet.goal
st.subheader(
    f"Wallet · ₹{float(wallet.equity):,.2f} "
    f"{'🎯 GOAL HIT' if goal_hit else ''}"
)
w1, w2, w3, w4 = st.columns(4)
w1.metric(
    "Initial capital",
    f"₹{float(wallet.initial):,.2f}",
    help="The starting pool of pretend money. Set on the Settings page.",
)
w2.metric(
    "Realised P&L (net)",
    f"₹{float(wallet.realised_net_pnl):,.2f}",
    help="Sum of net P&L (after charges) from every closed trade. Adds to "
         "the wallet — profits compound, losses shrink it.",
)
w3.metric(
    "Locked in open",
    f"₹{float(wallet.locked_in_open):,.2f}",
    help="Cash currently tied up in OPEN paper trades (qty × entry). Returns "
         "to the wallet when those trades close, plus or minus their net P&L.",
)
w4.metric(
    "Available to deploy",
    f"₹{float(wallet.available):,.2f}",
    help="The hard ceiling on the next trade. equity − locked. Risk gate "
         "rejects any signal whose notional exceeds this.",
)

# Progress bar toward the goal. Bar maxes at 100% but the caption shows the
# real ratio so over/undershoots are visible.
goal_span = float(wallet.goal - wallet.initial)
progress = max(0.0, min(1.0, wallet.progress_pct / 100))
st.progress(progress)
goal_caption = (
    f"₹{float(wallet.equity):,.2f} of ₹{float(wallet.goal):,.2f} goal "
    f"({wallet.progress_pct:.1f}% of the way) · "
    f"need ₹{max(0.0, float(wallet.goal - wallet.equity)):,.2f} more"
)
if wallet.drawdown_pct < 0:
    goal_caption += f" · drawdown {wallet.drawdown_pct:.1f}%"
st.caption(goal_caption)

st.divider()

# ───────────────────────── status row ─────────────────────────
c1, c2, c3, c4 = st.columns(4)
c1.metric("IST now", fmt_clock(now_ist))
c2.metric("Open paper trades", len(state["open_trades"]),
          f"max {risk_limits.max_open_positions}")
c3.metric("Closed today", len(state["closed_today"]))
c4.metric("Net P&L today (after charges)", f"₹{float(today['net_pnl_today']):,.2f}",
          delta_color="normal")

# ───────────────────────── today's money flow ─────────────────────────
st.caption("Today")
t1, t2, t3, t4, t5 = st.columns(5)
t1.metric("Investment (open)", f"₹{float(today['open_investment']):,.2f}",
          help="Capital currently deployed across open paper positions "
               "(qty × entry price).")
t2.metric("Purchases today", f"₹{float(today['purchases_today']):,.2f}",
          help="Total rupees BOUGHT today — entry legs of long trades opened "
               "today plus exit legs of any short trades closed today.")
t3.metric("Sellings today", f"₹{float(today['sellings_today']):,.2f}",
          help="Total rupees SOLD today — exit legs of long trades closed "
               "today plus entry legs of any short trades opened today.")
t4.metric("Charges today", f"₹{float(today['charges_today']):,.2f}",
          help="Round-trip Zerodha-MIS charges on trades closed today "
               "(brokerage + STT + exchange + SEBI + stamp + GST).")
t5.metric("Gross P&L today", f"₹{float(today['gross_pnl_today']):,.2f}",
          help="Price-difference P&L on trades closed today, before charges.")

# ───────────────────────── lifetime money flow ─────────────────────────
st.caption("Lifetime")
l1, l2, l3, l4, l5 = st.columns(5)
l1.metric("Trades", int(lifetime["closed_trades"]),
          help="Total closed paper trades since inception.")
l2.metric("Total purchases", f"₹{float(lifetime['purchases']):,.2f}",
          help="Lifetime sum of all rupees bought (long entries + short exits).")
l3.metric("Total sellings", f"₹{float(lifetime['sellings']):,.2f}",
          help="Lifetime sum of all rupees sold (long exits + short entries).")
l4.metric("Charges", f"₹{float(lifetime['charges']):,.2f}",
          help="Lifetime sum of all round-trip charges. Legacy trades booked "
               "before the charge model was added contribute ₹0.")
l5.metric("Net P&L", f"₹{float(lifetime['net_pnl']):,.2f}",
          f"gross ₹{float(lifetime['gross_pnl']):,.2f}",
          delta_color="off",
          help="Cumulative P&L after charges. Delta shows pre-charge gross.")

# Trading window indicator
if now_ist.weekday() >= 5:
    st.info("Weekend — market closed.")
elif now_ist.time() < TRADING_START:
    st.info(f"Pre-market. Trading window opens at {fmt_window(TRADING_START)}.")
elif now_ist.time() > SQUARE_OFF_AT:
    st.info(f"Post square-off ({fmt_window(SQUARE_OFF_AT)}). No new entries today.")
elif now_ist.time() > TRADING_END:
    st.info(f"Past entry cutoff ({fmt_window(TRADING_END)}). Existing positions only.")
else:
    st.success("Trading window open.")

st.divider()

# ───────────────────────── manual controls ─────────────────────────
with st.expander("Manual controls", expanded=False):
    st.caption(
        "Cron runs the decider every 2 min during the trading window. Use this "
        "to trigger an out-of-band run (bypasses the window check). Same code "
        "path as cron, same `LLM_MODE` from `.env`."
    )
    col_a, col_b = st.columns([2, 1])
    sig_id_input = col_a.text_input(
        "Signal ID (blank = all unconsumed today)",
        value="",
        placeholder="e.g. 42",
        key="manual_sig_id",
    )
    run_btn = col_b.button(
        "Run decider now",
        type="primary",
        use_container_width=True,
        key="run_decider_btn",
    )
    if run_btn:
        cmd = [
            str(VENV_PYTHON),
            str(REPO_ROOT / "scripts" / "decide_signals.py"),
            "--force-window",
        ]
        sid = sig_id_input.strip()
        if sid:
            if not sid.isdigit():
                st.error(f"Signal ID must be a positive integer, got {sid!r}.")
                st.stop()
            cmd += ["--signal-id", sid]
        # Inherit dashboard env (.env loaded via load_dotenv elsewhere is process-
        # local; the subprocess re-loads .env itself).
        with st.spinner("Running decider…"):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(REPO_ROOT),
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                st.error("Decider timed out after 300s — check logs.")
                st.stop()
        if proc.returncode == 0:
            st.success(f"Decider exited 0 · {fmt_clock()}")
        else:
            st.error(f"Decider exited {proc.returncode}")
        if proc.stdout.strip():
            st.code(proc.stdout, language="text")
        if proc.stderr.strip():
            st.code(proc.stderr, language="text")
        # Bust the bot-state cache so freshly-written decisions/trades appear.
        fetch_bot_state.clear()

# ───────────────────────── data freshness ─────────────────────────
st.subheader("Data freshness")
if state["latest_ticks"]:
    df = pd.DataFrame(state["latest_ticks"])
    df["candles_today"] = df["symbol"].map(lambda s: state["candle_counts"].get(s, 0))
    df["age_sec"] = df["ts"].apply(lambda t: int((now_ist - t.astimezone(IST)).total_seconds()))
    df["ts"] = df["ts"].apply(fmt_ist)
    df = df[["symbol", "ltp", "ts", "age_sec", "candles_today"]]
    df.columns = ["Symbol", "Last LTP", "Tick at", "Age (s)", "Candles today"]
    wrapped_table(df)
else:
    st.warning("No ticks yet. The poller runs every minute during market hours.")

# ───────────────────────── signals ─────────────────────────
st.subheader("Today's signals")
if state["signals_today"]:
    df = pd.DataFrame(state["signals_today"])
    df["ts"] = df["ts"].apply(fmt_ist)
    df = df[["id", "ts", "strategy", "symbol", "side", "entry_price", "stop_loss", "target", "consumed", "rationale"]]
    df.columns = ["#", "Time", "Strategy", "Symbol", "Side", "Entry", "Stop", "Target", "Acted", "Rationale"]
    wrapped_table(df)
else:
    st.caption("No signals yet today.")

# ───────────────────────── open positions ─────────────────────────
st.subheader("Open paper positions")
if state["open_trades"]:
    df = pd.DataFrame(state["open_trades"])
    df["entry_ts"] = df["entry_ts"].apply(fmt_ist)
    df = df[["id", "symbol", "side", "qty", "entry_price", "entry_ts", "stop_loss", "target"]]
    df.columns = ["#", "Symbol", "Side", "Qty", "Entry", "At", "Stop", "Target"]
    wrapped_table(df)
else:
    st.caption("None open.")

# ───────────────────────── closed today ─────────────────────────
st.subheader("Closed today")
if state["closed_today"]:
    df = pd.DataFrame(state["closed_today"])
    df["entry_ts"] = df["entry_ts"].apply(fmt_ist)
    df["exit_ts"] = df["exit_ts"].apply(fmt_ist)
    df = df[["symbol", "side", "qty", "entry_price", "exit_price", "exit_reason",
             "pnl_inr", "charges_inr", "net_pnl_inr", "entry_ts", "exit_ts"]]
    df.columns = ["Symbol", "Side", "Qty", "Entry", "Exit", "Why",
                  "Gross P&L (₹)", "Charges (₹)", "Net P&L (₹)", "In", "Out"]
    wrapped_table(df)
else:
    st.caption("None closed yet today.")

# ───────────────────────── audit ─────────────────────────
with st.expander("Recent audit log (last 25)"):
    if state["recent_audit"]:
        df = pd.DataFrame(state["recent_audit"])
        df["ts"] = df["ts"].apply(fmt_ist)
        wrapped_table(df)
    else:
        st.caption("Empty.")

st.caption(f"Cached 15s · {fmt_clock()} · refresh page to re-pull")

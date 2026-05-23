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

from helm.config import (
    HOUSE_TRADE_FILTER,
    SQUARE_OFF_AT,
    TRADING_END,
    TRADING_START,
    WATCHLIST,
    live_risk_limits,
)
from helm.kite_health import token_health
from helm.wallet import wallet_state
from helm.dashboard.format import IST, fmt_clock, fmt_ist, wrapped_table
from helm.dashboard.theme import apply_theme, kpi_card, kpi_grid, page_header, pill
from helm.data.store import conn

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

st.set_page_config(page_title="Helm — Intraday", page_icon="🧭", layout="wide")
apply_theme()


def _market_status(now: datetime) -> tuple[str, str]:
    """Compact (label, pill-kind) for the header — mirrors the window banner."""
    if now.weekday() >= 5:
        return "Market closed · weekend", "closed"
    t = now.time()
    if t < TRADING_START:
        return "Pre-market", "info"
    if t > SQUARE_OFF_AT:
        return "Square-off complete", "closed"
    if t > TRADING_END:
        return "Positions only", "warn"
    return "Market open", "live"


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
    """Calendar-aware verdict on the daily token refresh (helm.kite_health).

    Weekend-aware: Friday's refresh stays "ok" all weekend, so the banner only
    fires when a *due* weekday refresh is actually missing or failed.
    """
    return token_health()


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
            f"FROM signals WHERE {today_filter} AND {HOUSE_TRADE_FILTER} ORDER BY ts DESC"
        ))
        open_trades = list(c.execute(
            "SELECT id, symbol, side, qty, entry_price, entry_ts, stop_loss, target "
            f"FROM paper_trades WHERE status = 'OPEN' AND {HOUSE_TRADE_FILTER} "
            "ORDER BY entry_ts DESC"
        ))
        closed_today = list(c.execute(
            f"SELECT symbol, side, qty, entry_price, exit_price, exit_reason, "
            f"pnl_inr, charges_inr, net_pnl_inr, entry_ts, exit_ts "
            f"FROM paper_trades WHERE status = 'CLOSED' AND exit_ts >= {today_ist} "
            f"AND {HOUSE_TRADE_FILTER} ORDER BY exit_ts DESC"
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
            WHERE {HOUSE_TRADE_FILTER}
            """
        ).fetchone()
        lifetime_totals = c.execute(
            f"""
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
            WHERE {HOUSE_TRADE_FILTER}
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


# ───────────────────────── navbar + header ─────────────────────────
# Fetch the broker account first so the logged-in user can sit in the navbar.
_status_label, _status_kind = _market_status(datetime.now(IST))
acct = fetch_account()

_nav_user = _nav_user_sub = None
if "error" not in acct:
    p = acct["profile"]
    cash = float(acct["margins"].get("available", {}).get("cash", 0) or 0)
    _nav_user = p.get("user_name") or p.get("user_id")
    _nav_user_sub = f"{p.get('user_id')} · cash ₹{cash:,.0f}"

_wl = list(WATCHLIST)
_wl_label = " · ".join(_wl[:5]) + (f"  +{len(_wl) - 5} more" if len(_wl) > 5 else "")
page_header(
    "Intraday Trading Desk",
    f"Paper mode · watching {_wl_label}",
    status=_status_label,
    status_kind=_status_kind,
    user=_nav_user,
    user_sub=_nav_user_sub,
)

# Broker connection problems still need a visible banner.
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

# Kite auto-login watchdog — flags silent cron failures (calendar-aware, so it
# stays quiet over weekends when Friday's refresh is the expected latest).
health = fetch_token_health()
if health["level"] == "never":
    st.warning(
        "Kite auto-login has never run. Confirm the cron entry "
        "(`crontab -l | grep kite_auto_login`) and that .env has "
        "KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET set."
    )
elif health["level"] == "failed":
    err = (health["detail"] or {}).get("error", "unknown")
    st.error(
        f"Kite auto-login last attempt FAILED at {fmt_ist(health['ts'])} — {err}. "
        f"Check logs/kite_login.log; run `python scripts/kite_auto_login.py` to retry."
    )
elif health["level"] == "stale":
    st.error(
        f"Kite token refresh is overdue — last success {fmt_ist(health['ts'])} "
        f"({health['age_h']:.0f}h ago), but a refresh was due at "
        f"{fmt_ist(health['expected'])}. The weekday 06:10 IST cron likely "
        f"failed silently — check logs/kite_login.log or run "
        f"`python scripts/kite_auto_login.py`."
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
_day_pnl = float(today["net_pnl_today"])
_real_pnl = float(wallet.realised_net_pnl)
_day_pill = pill(
    f"{'▲' if _day_pnl >= 0 else '▼'} ₹{_day_pnl:,.2f} today",
    "pos" if _day_pnl >= 0 else "neg", dot=False,
)
_goal_pill = (
    pill("🎯 Goal reached", "pos", dot=False) if goal_hit
    else pill(f"{wallet.progress_pct:.1f}% to goal", "info", dot=False)
)
st.markdown(
    f"""
    <div class="helm-hero">
      <div>
        <div class="helm-hero-label">Wallet equity</div>
        <div class="helm-hero-value">₹{float(wallet.equity):,.2f}</div>
        <div class="helm-hero-meta">
          Realised net P&amp;L <b>₹{_real_pnl:,.2f}</b> ·
          available to deploy <b>₹{float(wallet.available):,.2f}</b>
        </div>
      </div>
      <div class="helm-hero-side">{_day_pill}{_goal_pill}</div>
    </div>
    """,
    unsafe_allow_html=True,
)
_real = float(wallet.realised_net_pnl)
kpi_grid([
    kpi_card(
        "Initial capital", f"₹{float(wallet.initial):,.2f}", icon="bank",
        help="The starting pool of pretend money. Set on the Settings page.",
    ),
    kpi_card(
        "Realised P&L (net)", f"₹{_real:,.2f}", icon="wallet",
        sub="▲ in profit" if _real >= 0 else "▼ in loss",
        sub_kind="pos" if _real >= 0 else "neg",
        help="Sum of net P&L (after charges) from every closed trade. Adds to "
             "the wallet — profits compound, losses shrink it.",
    ),
    kpi_card(
        "Locked in open", f"₹{float(wallet.locked_in_open):,.2f}", icon="lock",
        help="Cash currently tied up in OPEN paper trades (qty × entry). Returns "
             "to the wallet when those trades close, plus or minus their net P&L.",
    ),
    kpi_card(
        "Available to deploy", f"₹{float(wallet.available):,.2f}", icon="rocket",
        help="The hard ceiling on the next trade. equity − locked. Risk gate "
             "rejects any signal whose notional exceeds this.",
    ),
])

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
_npt = float(today["net_pnl_today"])
kpi_grid([
    kpi_card("IST now", fmt_clock(now_ist), icon="clock"),
    kpi_card("Open paper trades", f"{len(state['open_trades'])}", icon="folder",
             sub=f"max {risk_limits.max_open_positions}", sub_kind="muted"),
    kpi_card("Closed today", f"{len(state['closed_today'])}", icon="check"),
    kpi_card("Net P&L today", f"₹{_npt:,.2f}", icon="trend-up" if _npt >= 0 else "trend-down",
             sub="after charges", sub_kind="pos" if _npt >= 0 else "neg"),
])

# ───────────────────────── today's money flow ─────────────────────────
st.caption("Today")
kpi_grid([
    kpi_card("Investment (open)", f"₹{float(today['open_investment']):,.2f}", icon="pin",
             help="Capital currently deployed across open paper positions "
                  "(qty × entry price)."),
    kpi_card("Purchases today", f"₹{float(today['purchases_today']):,.2f}", icon="buy",
             help="Total rupees BOUGHT today — entry legs of long trades opened "
                  "today plus exit legs of any short trades closed today."),
    kpi_card("Sellings today", f"₹{float(today['sellings_today']):,.2f}", icon="sell",
             help="Total rupees SOLD today — exit legs of long trades closed "
                  "today plus entry legs of any short trades opened today."),
    kpi_card("Charges today", f"₹{float(today['charges_today']):,.2f}", icon="receipt",
             help="Round-trip Zerodha-MIS charges on trades closed today "
                  "(brokerage + STT + exchange + SEBI + stamp + GST)."),
    kpi_card("Gross P&L today", f"₹{float(today['gross_pnl_today']):,.2f}", icon="bar",
             help="Price-difference P&L on trades closed today, before charges."),
], cols=5)

# ───────────────────────── lifetime money flow ─────────────────────────
st.caption("Lifetime")
_life_net = float(lifetime["net_pnl"])
kpi_grid([
    kpi_card("Trades", f"{int(lifetime['closed_trades']):,}", icon="repeat",
             help="Total closed paper trades since inception."),
    kpi_card("Total purchases", f"₹{float(lifetime['purchases']):,.2f}", icon="buy",
             help="Lifetime sum of all rupees bought (long entries + short exits)."),
    kpi_card("Total sellings", f"₹{float(lifetime['sellings']):,.2f}", icon="sell",
             help="Lifetime sum of all rupees sold (long exits + short entries)."),
    kpi_card("Charges", f"₹{float(lifetime['charges']):,.2f}", icon="receipt",
             help="Lifetime sum of all round-trip charges. Legacy trades booked "
                  "before the charge model was added contribute ₹0."),
    kpi_card("Net P&L", f"₹{_life_net:,.2f}", icon="trophy",
             sub=f"gross ₹{float(lifetime['gross_pnl']):,.2f}",
             sub_kind="pos" if _life_net >= 0 else "neg",
             help="Cumulative P&L after charges. Sub-line shows pre-charge gross."),
], cols=5)

# (Market-status banner removed — it's shown once in the navbar status pill.)

st.divider()

# ───────────────────────── manual controls ─────────────────────────
with st.expander("Manual controls", expanded=False):
    st.caption(
        "Cron runs the decider every 2 min during the trading window. Use this "
        "to trigger an out-of-band run (bypasses the window check). Same code "
        "path as cron, same `LLM_MODE` from `.env`."
    )
    # vertical_alignment="bottom" lines the button up with the input box
    # (whose label adds height above it) instead of the column top.
    col_a, col_b = st.columns([2, 1], vertical_alignment="bottom")
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

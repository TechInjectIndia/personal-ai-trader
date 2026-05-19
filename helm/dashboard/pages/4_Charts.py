"""Charts — candlesticks for each watchlist symbol with signal markers.

For the chosen symbol and date, plots 1-min OHLC candles from candles_1m
and overlays:
  * Signal markers (where a strategy fired today)
  * Open paper trade entry/stop/target lines (if any active in that day)
  * The 15-min ORB range (high/low of the first 15 min) since orb_15m is
    one of our active strategies — useful to eyeball whether breakouts
    looked decisive
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from helm.config import MARKET_OPEN, WATCHLIST
from helm.dashboard.format import wrapped_table
from helm.data.store import conn

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="Helm — Charts", page_icon="📈", layout="wide")
st.title("Charts — candles + strategy overlays")
st.caption(
    "1-minute candlesticks for the selected stock and day. Signal markers show "
    "where strategies fired, and orange dashed lines mark the 15-minute Opening "
    "Range used by the ORB strategy."
)

# ───────────────────────── controls ─────────────────────────
c1, c2, c3 = st.columns([1, 1, 2])
symbol = c1.selectbox("Stock", WATCHLIST, index=0)
day = c2.date_input("Date", date.today(), max_value=date.today())
show_orb = c3.checkbox("Show 15-min Opening Range overlay", value=True)


# ───────────────────────── data ─────────────────────────
@st.cache_data(ttl=10, show_spinner=False)
def fetch_candles(symbol: str, day: date) -> pd.DataFrame:
    start_ist = datetime.combine(day, time(0, 0), tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT bar_ts, open, high, low, close, tick_count
            FROM candles_1m
            WHERE symbol = %s AND bar_ts >= %s AND bar_ts < %s
            ORDER BY bar_ts ASC
            """,
            (symbol, start_ist, end_ist),
        ))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["bar_ts"] = pd.to_datetime(df["bar_ts"]).dt.tz_convert(IST)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


@st.cache_data(ttl=10, show_spinner=False)
def fetch_signals(symbol: str, day: date) -> list[dict]:
    start_ist = datetime.combine(day, time(0, 0), tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    with conn() as c:
        return list(c.execute(
            """
            SELECT s.id, s.ts, s.strategy, s.side, s.entry_price, s.stop_loss,
                   s.target, s.consumed, s.rationale,
                   d.verdict, d.reasoning AS d_reason
            FROM signals s
            LEFT JOIN decisions d ON d.signal_id = s.id
            WHERE s.symbol = %s AND s.ts >= %s AND s.ts < %s
            ORDER BY s.ts ASC
            """,
            (symbol, start_ist, end_ist),
        ))


@st.cache_data(ttl=10, show_spinner=False)
def fetch_trades(symbol: str, day: date) -> list[dict]:
    start_ist = datetime.combine(day, time(0, 0), tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    with conn() as c:
        return list(c.execute(
            """
            SELECT id, side, qty, entry_price, exit_price, entry_ts, exit_ts,
                   stop_loss, target, exit_reason, pnl_inr, status
            FROM paper_trades
            WHERE symbol = %s AND entry_ts >= %s AND entry_ts < %s
            ORDER BY entry_ts ASC
            """,
            (symbol, start_ist, end_ist),
        ))


candles = fetch_candles(symbol, day)
signals = fetch_signals(symbol, day)
trades = fetch_trades(symbol, day)

if candles.empty:
    st.warning(f"No candles for {symbol} on {day.isoformat()}. "
               "Either the market wasn't open that day, or the bot wasn't running yet.")
    st.stop()

# ───────────────────────── chart ─────────────────────────
fig = go.Figure()
fig.add_trace(go.Candlestick(
    x=candles["bar_ts"],
    open=candles["open"], high=candles["high"],
    low=candles["low"], close=candles["close"],
    name="1-min OHLC",
    increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
))

# 15-min Opening Range overlay (high/low of first 15 bars after market open)
if show_orb:
    open_dt = datetime.combine(day, MARKET_OPEN, tzinfo=IST)
    orb_window_end = open_dt + timedelta(minutes=15)
    orb_bars = candles[(candles["bar_ts"] >= open_dt) & (candles["bar_ts"] < orb_window_end)]
    if not orb_bars.empty:
        orb_high = float(orb_bars["high"].max())
        orb_low = float(orb_bars["low"].min())
        x_full = [candles["bar_ts"].min(), candles["bar_ts"].max()]
        fig.add_trace(go.Scatter(
            x=x_full, y=[orb_high, orb_high],
            mode="lines", name=f"ORB high {orb_high:.2f}",
            line=dict(color="orange", width=1, dash="dash"),
        ))
        fig.add_trace(go.Scatter(
            x=x_full, y=[orb_low, orb_low],
            mode="lines", name=f"ORB low {orb_low:.2f}",
            line=dict(color="orange", width=1, dash="dash"),
        ))

# Signal markers — triangle up for BUY, down for SELL, color by verdict
for s in signals:
    ts = s["ts"].astimezone(IST)
    entry = float(s["entry_price"])
    is_buy = s["side"] == "BUY"
    verdict = (s.get("verdict") or "").upper()
    color = "#26a69a" if verdict == "TAKE" else ("#ef5350" if verdict == "SKIP" else "#888")
    label = f"{s['strategy']} {s['side']} #{s['id']} → {verdict or 'pending'}"
    fig.add_trace(go.Scatter(
        x=[ts], y=[entry],
        mode="markers",
        marker=dict(
            symbol="triangle-up" if is_buy else "triangle-down",
            size=14, color=color,
            line=dict(color="black", width=1),
        ),
        name=label,
        hovertemplate=(
            f"<b>{label}</b><br>"
            f"Entry: ₹{entry:.2f}<br>"
            f"Stop: ₹{float(s['stop_loss']):.2f}<br>"
            f"Target: ₹{float(s['target']):.2f}<br>"
            if s.get("target") is not None else
            f"<b>{label}</b><br>Entry: ₹{entry:.2f}<br>Stop: ₹{float(s['stop_loss']):.2f}<br>"
        ) + "<extra></extra>",
        showlegend=True,
    ))

# Trade entry/exit markers + stop/target lines for each trade
for t in trades:
    entry_ts = t["entry_ts"].astimezone(IST)
    entry_px = float(t["entry_price"])
    fig.add_trace(go.Scatter(
        x=[entry_ts], y=[entry_px],
        mode="markers",
        marker=dict(symbol="circle", size=10, color="blue",
                    line=dict(color="white", width=1)),
        name=f"Trade #{t['id']} entry",
        hovertemplate=f"<b>Trade #{t['id']} entry</b><br>{t['side']} {t['qty']} @ ₹{entry_px:.2f}<extra></extra>",
    ))
    if t["status"] == "CLOSED" and t["exit_ts"] is not None:
        exit_ts = t["exit_ts"].astimezone(IST)
        exit_px = float(t["exit_price"])
        pnl = float(t["pnl_inr"])
        sym = "x"
        col = "#26a69a" if pnl >= 0 else "#ef5350"
        fig.add_trace(go.Scatter(
            x=[exit_ts], y=[exit_px],
            mode="markers",
            marker=dict(symbol=sym, size=12, color=col,
                        line=dict(color="white", width=1)),
            name=f"Trade #{t['id']} exit ({t['exit_reason']})",
            hovertemplate=(
                f"<b>Trade #{t['id']} exit · {t['exit_reason']}</b><br>"
                f"@ ₹{exit_px:.2f}<br>P&L ₹{pnl:.2f}<extra></extra>"
            ),
        ))

fig.update_layout(
    height=560,
    xaxis_title=None, yaxis_title="Price (₹)",
    xaxis_rangeslider_visible=False,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    margin=dict(l=20, r=20, t=20, b=20),
    hovermode="x unified",
)
st.plotly_chart(fig, use_container_width=True)

# ───────────────────────── side-tables ─────────────────────────
st.subheader("Signals on this day")
if signals:
    sig_df = pd.DataFrame([
        {
            "#": s["id"],
            "Time": s["ts"].astimezone(IST).strftime("%H:%M"),
            "Strategy": s["strategy"],
            "Side": s["side"],
            "Entry": float(s["entry_price"]),
            "Stop": float(s["stop_loss"]),
            "Target": float(s["target"]) if s["target"] is not None else None,
            "Verdict": s.get("verdict") or "—",
            "Reason": (s.get("d_reason") or s.get("rationale") or "")[:140],
        }
        for s in signals
    ])
    wrapped_table(sig_df)
else:
    st.caption("No signals fired for this stock on this day.")

st.subheader("Paper trades on this day")
if trades:
    tr_df = pd.DataFrame([
        {
            "#": t["id"],
            "Side": t["side"],
            "Qty": t["qty"],
            "Entry": float(t["entry_price"]),
            "Exit": float(t["exit_price"]) if t["exit_price"] is not None else None,
            "Why": t["exit_reason"] or "",
            "P&L (₹)": float(t["pnl_inr"]) if t["pnl_inr"] is not None else None,
            "In": t["entry_ts"].astimezone(IST).strftime("%H:%M:%S"),
            "Out": t["exit_ts"].astimezone(IST).strftime("%H:%M:%S") if t["exit_ts"] else "",
            "Status": t["status"],
        }
        for t in trades
    ])
    wrapped_table(tr_df)
else:
    st.caption("No paper trades booked for this stock on this day.")

st.divider()
st.caption(
    f"{len(candles)} candle(s) · {len(signals)} signal(s) · "
    f"{len(trades)} trade(s) · cached 10s"
)

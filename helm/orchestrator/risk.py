"""
Pre-trade risk gate.

Single source of truth for whether a proposed paper trade is allowed. Called
from paper_execute.py before any signal is acted on. Returns (allowed, reason).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from helm.config import dynamic_position_cap, live_risk_limits
from helm.data.store import conn
from helm.wallet import wallet_state

IST = ZoneInfo("Asia/Kolkata")


def _today_ist() -> date:
    return datetime.now(IST).date()


def open_paper_positions() -> int:
    with conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM paper_trades WHERE status = 'OPEN'").fetchone()
    return int(row["n"])


def todays_realized_pnl() -> Decimal:
    """Sum of pnl_inr for trades closed today (IST)."""
    with conn() as c:
        row = c.execute(
            """
            SELECT COALESCE(SUM(pnl_inr), 0) AS total
            FROM paper_trades
            WHERE status = 'CLOSED'
              AND exit_ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
            """
        ).fetchone()
    return Decimal(row["total"])


def kill_engaged_today() -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT kill_engaged FROM daily_state WHERE trade_date = %s",
            (_today_ist(),),
        ).fetchone()
    return bool(row and row["kill_engaged"])


def has_open_position(symbol: str) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM paper_trades WHERE status = 'OPEN' AND symbol = %s LIMIT 1",
            (symbol,),
        ).fetchone()
    return row is not None


def signals_for_symbol_today(symbol: str) -> int:
    with conn() as c:
        row = c.execute(
            """
            SELECT COUNT(*) AS n
            FROM paper_trades
            WHERE symbol = %s
              AND entry_ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
            """,
            (symbol,),
        ).fetchone()
    return int(row["n"])


def evaluate(symbol: str, side: str, qty: int, entry_price: Decimal) -> tuple[bool, str]:
    """Return (allowed, reason). Reason is human-readable, used for audit."""
    limits = live_risk_limits()

    if kill_engaged_today():
        return False, "kill switch engaged for today"

    realized = todays_realized_pnl()
    if realized <= -limits.daily_loss_kill_inr:
        return False, f"daily loss kill hit ({realized} ≤ -{limits.daily_loss_kill_inr})"

    if open_paper_positions() >= limits.max_open_positions:
        return False, f"max_open_positions={limits.max_open_positions} reached"

    if has_open_position(symbol):
        return False, f"already have an open position in {symbol}"

    if signals_for_symbol_today(symbol) >= limits.max_signals_per_symbol_per_day:
        return False, f"already traded {symbol} max times today"

    # The per-trade cap scales with realised pnl (see dynamic_position_cap),
    # so we recompute the effective ceiling here rather than using the raw
    # limits.max_position_inr — otherwise winners couldn't be sized up.
    wallet = wallet_state()
    effective_cap = dynamic_position_cap(wallet.realised_net_pnl, limits.max_position_inr)
    notional = entry_price * qty
    if notional > effective_cap:
        return False, (f"notional ₹{notional} exceeds dynamic per-trade cap "
                       f"₹{effective_cap} (base ₹{limits.max_position_inr})")

    # Wallet is the hard ceiling: a trade can never be opened for more cash
    # than the pool currently has free. This is what makes the bot
    # self-bounded — losses shrink the pool, profits grow it, and no trade
    # can go beyond what's actually there.
    if notional > wallet.available:
        return False, (
            f"notional ₹{notional} exceeds available wallet cash ₹{wallet.available} "
            f"(equity ₹{wallet.equity} − locked ₹{wallet.locked_in_open})"
        )

    return True, "ok"

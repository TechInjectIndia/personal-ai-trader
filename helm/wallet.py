"""
Single-pool wallet accounting.

The bot trades from one cash pool that starts at `initial_capital_inr` and
grows/shrinks with realised net P&L (i.e. after Zerodha-MIS charges). Open
positions lock their entry notional out of the pool until they close. New
trades must fit in whatever is left.

This file is the single source of truth for "how much can the bot spend right
now?" — both the risk gate and the position sizer call into it.

Definitions
-----------
* equity      = initial_capital + Σ realised_net_pnl(closed)
                  (realised P&L only; we don't mark-to-market unrealised P&L)
* locked      = Σ qty × entry_price  over OPEN trades
* available   = equity - locked
* goal        = configured target wallet value (default 2× initial)
* progress    = (equity - initial) / (goal - initial), clamped to [0, 1]

For legacy closed trades booked before the charge model existed, charges_inr
is NULL; we fall back to gross pnl_inr for those rows so equity stays
self-consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from helm.config import live_wallet_config
from helm.data.store import conn


@dataclass(frozen=True)
class WalletState:
    initial: Decimal
    realised_net_pnl: Decimal
    equity: Decimal
    locked_in_open: Decimal
    available: Decimal
    goal: Decimal
    progress_pct: float        # equity vs goal, 0..100 (can exceed 100 if goal hit)
    drawdown_pct: float        # negative if below initial, 0 otherwise


def wallet_state() -> WalletState:
    cfg = live_wallet_config()
    initial = cfg.initial_capital_inr
    goal = cfg.goal_capital_inr

    with conn() as c:
        realised = c.execute(
            """
            SELECT COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr)), 0) AS pnl
            FROM paper_trades
            WHERE status = 'CLOSED'
            """
        ).fetchone()["pnl"]
        locked = c.execute(
            """
            SELECT COALESCE(SUM(qty * entry_price), 0) AS locked
            FROM paper_trades
            WHERE status = 'OPEN'
            """
        ).fetchone()["locked"]

    realised = Decimal(realised)
    locked = Decimal(locked)
    equity = initial + realised
    available = equity - locked

    span = goal - initial
    if span > 0:
        progress = float((equity - initial) / span * 100)
    else:
        progress = 0.0

    drawdown = 0.0 if equity >= initial else float((equity - initial) / initial * 100)

    return WalletState(
        initial=initial,
        realised_net_pnl=realised,
        equity=equity,
        locked_in_open=locked,
        available=available,
        goal=goal,
        progress_pct=progress,
        drawdown_pct=drawdown,
    )

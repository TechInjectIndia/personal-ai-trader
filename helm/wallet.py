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

from helm.config import HOUSE_TRADE_FILTER, live_wallet_config
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


def _market_wallet_config(market: str) -> tuple[Decimal, Decimal]:
    """(initial, goal) capital for a market. IN keeps using
    live_wallet_config() (settings-overridable, byte-identical); other markets
    read the per-market `wallets` table (currency-native amounts)."""
    if market == "IN":
        cfg = live_wallet_config()
        return cfg.initial_capital_inr, cfg.goal_capital_inr
    with conn() as c:
        row = c.execute(
            "SELECT initial_capital, goal_capital FROM wallets WHERE market = %s",
            (market,),
        ).fetchone()
    if not row:
        return Decimal("0"), Decimal("0")
    goal = Decimal(row["goal_capital"]) if row["goal_capital"] is not None else Decimal("0")
    return Decimal(row["initial_capital"]), goal


def wallet_state(market: str = "IN") -> WalletState:
    initial, goal = _market_wallet_config(market)

    # House = the incumbent's own rows only (NULL or 'house-claude'); competitor
    # league trades must never leak into the house wallet. Scoped to one market
    # so INR and USD books never co-mingle. For IN (every legacy row is 'IN')
    # this is identical to the previous query.
    with conn() as c:
        realised = c.execute(
            f"""
            SELECT COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr)), 0) AS pnl
            FROM paper_trades
            WHERE status = 'CLOSED' AND market = %s AND {HOUSE_TRADE_FILTER}
            """,
            (market,),
        ).fetchone()["pnl"]
        locked = c.execute(
            f"""
            SELECT COALESCE(SUM(qty * entry_price), 0) AS locked
            FROM paper_trades
            WHERE status = 'OPEN' AND market = %s AND {HOUSE_TRADE_FILTER}
            """,
            (market,),
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

"""Per-competitor wallet accounting.

The single-pool `helm.wallet.wallet_state()` powers the incumbent house bot and
sums *all* paper_trades regardless of competitor. This module is its
competitor-scoped sibling: it computes the same WalletState shape but filtered
to one competitor's `competitor_id`, with the initial capital read from that
competitor's `competitor_wallets` row.

Authoritative state is always *recomputed from `paper_trades`* (the source of
truth), exactly like the house wallet — the `competitor_wallets.available_inr` /
`realized_pnl_inr` columns are a denormalised cache for the dashboard, refreshed
opportunistically via `sync_wallet_cache()`. We never bookkeep money twice.

Definitions match helm.wallet:
  equity    = initial + Σ realised_net_pnl(closed for this competitor)
  locked    = Σ qty × entry_price over this competitor's OPEN trades
  available = equity − locked
"""

from __future__ import annotations

from decimal import Decimal

from helm.data.store import conn
from helm.wallet import WalletState


def competitor_initial_capital(competitor_id: str) -> Decimal:
    """Initial capital for a competitor, from its competitor_wallets row.

    Raises ValueError if the competitor has no wallet (callers should seed one
    before trading — see scripts/seed_competitors.py).
    """
    with conn() as c:
        row = c.execute(
            "SELECT initial_capital_inr FROM competitor_wallets WHERE competitor_id = %s",
            (competitor_id,),
        ).fetchone()
    if not row or row["initial_capital_inr"] is None:
        raise ValueError(f"no wallet for competitor {competitor_id!r}")
    return Decimal(row["initial_capital_inr"])


def competitor_wallet_state(competitor_id: str) -> WalletState:
    """WalletState for one competitor, recomputed from its paper_trades.

    Mirrors helm.wallet.wallet_state() but scoped by competitor_id. The goal is
    fixed at 2× initial (the league's "double it" target) since competitors
    don't carry the dashboard-editable WalletConfig.goal override.
    """
    initial = competitor_initial_capital(competitor_id)
    goal = initial * 2

    with conn() as c:
        realised = c.execute(
            """
            SELECT COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr)), 0) AS pnl
            FROM paper_trades
            WHERE status = 'CLOSED' AND competitor_id = %s
            """,
            (competitor_id,),
        ).fetchone()["pnl"]
        locked = c.execute(
            """
            SELECT COALESCE(SUM(qty * entry_price), 0) AS locked
            FROM paper_trades
            WHERE status = 'OPEN' AND competitor_id = %s
            """,
            (competitor_id,),
        ).fetchone()["locked"]

    realised = Decimal(realised)
    locked = Decimal(locked)
    equity = initial + realised
    available = equity - locked

    span = goal - initial
    progress = float((equity - initial) / span * 100) if span > 0 else 0.0
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


def sync_wallet_cache(competitor_id: str) -> WalletState:
    """Refresh the denormalised competitor_wallets cache from live trade state.

    Returns the freshly computed WalletState. Cheap; call after opening/closing
    a competitor trade so the dashboard's quick read stays close to truth.
    """
    state = competitor_wallet_state(competitor_id)
    with conn() as c:
        c.execute(
            """
            UPDATE competitor_wallets
            SET available_inr = %s,
                realized_pnl_inr = %s,
                updated_at = now()
            WHERE competitor_id = %s
            """,
            (state.available, state.realised_net_pnl, competitor_id),
        )
    return state

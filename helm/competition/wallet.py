"""Per-competitor, per-market wallet accounting.

The single-pool `helm.wallet.wallet_state()` powers the incumbent house bot. This
module is its competitor-scoped sibling: the same WalletState shape, filtered to
one competitor's `competitor_id` AND one `market`. Each (competitor, market) pair
has its own isolated wallet row in `competitor_wallets` (composite PK), so an
agent's INR book and its USD (US/crypto) books never mix currencies.

Authoritative state is always *recomputed from `paper_trades`* (the source of
truth) — the `competitor_wallets.available_inr` / `realized_pnl_inr` columns are
a denormalised cache, refreshed via `sync_wallet_cache()`. (The column names keep
the historical `_inr` suffix; the values are in the wallet's own `currency`.)

Definitions match helm.wallet, scoped to (competitor, market):
  equity    = initial + Σ realised_net_pnl(closed for this competitor+market)
  locked    = Σ qty × entry_price over this competitor's OPEN trades in market
  available = equity − locked
"""

from __future__ import annotations

from decimal import Decimal

from helm.config import MARKET_WALLET_SEED, WalletConfig
from helm.data.store import conn
from helm.wallet import WalletState


def _seed_amount(market: str) -> tuple[Decimal, str]:
    """(initial_capital, currency) for a competitor's wallet in `market`. IN keeps
    the league's ₹ seed; US/CRYPTO use the per-market seed (USD)."""
    if market == "IN":
        return WalletConfig().initial_capital_inr, "INR"
    seed = MARKET_WALLET_SEED.get(market)
    if seed:
        currency, initial, _goal = seed
        return Decimal(initial), currency
    return WalletConfig().initial_capital_inr, "INR"


def ensure_competitor_wallet(competitor_id: str, market: str = "IN") -> None:
    """Seed a competitor's per-market wallet if absent (idempotent). IN wallets
    are seeded by scripts/seed_competitors.py; this lets US/CRYPTO wallets spring
    into existence the first time an agent trades that market."""
    initial, currency = _seed_amount(market)
    with conn() as c:
        c.execute(
            """
            INSERT INTO competitor_wallets
                (competitor_id, market, currency, initial_capital_inr,
                 available_inr, realized_pnl_inr)
            VALUES (%s, %s, %s, %s, %s, 0)
            ON CONFLICT (competitor_id, market) DO NOTHING
            """,
            (competitor_id, market, currency, initial, initial),
        )


def competitor_initial_capital(competitor_id: str, market: str = "IN") -> Decimal:
    """Initial capital for a competitor's wallet in `market`. Non-IN wallets are
    seeded on demand; IN must already exist (raises ValueError otherwise — the
    incumbent contract, preserved)."""
    if market != "IN":
        ensure_competitor_wallet(competitor_id, market)
    with conn() as c:
        row = c.execute(
            "SELECT initial_capital_inr FROM competitor_wallets "
            "WHERE competitor_id = %s AND market = %s",
            (competitor_id, market),
        ).fetchone()
    if not row or row["initial_capital_inr"] is None:
        raise ValueError(f"no wallet for competitor {competitor_id!r} in {market}")
    return Decimal(row["initial_capital_inr"])


def competitor_wallet_state(competitor_id: str, market: str = "IN") -> WalletState:
    """WalletState for one competitor in one market, recomputed from paper_trades.

    Mirrors helm.wallet.wallet_state() scoped by (competitor_id, market). Goal is
    fixed at 2× initial (the league's "double it" target)."""
    initial = competitor_initial_capital(competitor_id, market)
    goal = initial * 2

    with conn() as c:
        realised = c.execute(
            """
            SELECT COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr)), 0) AS pnl
            FROM paper_trades
            WHERE status = 'CLOSED' AND competitor_id = %s AND market = %s
            """,
            (competitor_id, market),
        ).fetchone()["pnl"]
        locked = c.execute(
            """
            SELECT COALESCE(SUM(qty * entry_price), 0) AS locked
            FROM paper_trades
            WHERE status = 'OPEN' AND competitor_id = %s AND market = %s
            """,
            (competitor_id, market),
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


def sync_wallet_cache(competitor_id: str, market: str = "IN") -> WalletState:
    """Refresh the denormalised competitor_wallets cache for (competitor, market)
    from live trade state. Returns the freshly computed WalletState."""
    state = competitor_wallet_state(competitor_id, market)
    with conn() as c:
        c.execute(
            """
            UPDATE competitor_wallets
            SET available_inr = %s, realized_pnl_inr = %s, updated_at = now()
            WHERE competitor_id = %s AND market = %s
            """,
            (state.available, state.realised_net_pnl, competitor_id, market),
        )
    return state

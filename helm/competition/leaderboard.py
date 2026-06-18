"""League standings — one fair, equity-ranked row per competitor.

The dashboard's leaderboard and the league_status CLI both read from here so
there's a single source of truth for "who's winning". Every competitor is put
on the same ₹50k basis (its `competitor_wallets.initial_capital_inr`) and ranked
by current equity.

The one subtlety this module owns: the incumbent **house-claude** keeps booking
live cron trades with `competitor_id = NULL` (its rows were only backfilled to
'house-claude' once), so the house book is "NULL OR 'house-claude'"
(helm.config.HOUSE_TRADE_FILTER). Every other competitor is its own
`competitor_id` exactly. Using the wrong filter would make the house look empty,
so the filter is selected per row here.

Definitions match helm.wallet / helm.competition.wallet:
  equity     = initial + Σ realised_net_pnl(closed)
  locked     = Σ qty × entry_price over OPEN trades
  available  = equity − locked
  goal       = 2× initial ("double it")
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from helm.analytics.economics import book_economics
from helm.config import HOUSE_COMPETITOR_ID, HOUSE_TRADE_FILTER
from helm.data.store import conn


@dataclass
class LeaderRow:
    competitor_id: str
    name: str
    backend: str
    model: str | None
    autonomy_level: str
    status: str
    market: str
    currency: str
    initial: Decimal
    realised_net_pnl: Decimal
    equity: Decimal
    locked: Decimal
    available: Decimal
    open_positions: int
    trades: int
    wins: int
    losses: int
    win_rate_pct: float | None
    progress_pct: float
    drawdown_pct: float
    rank: int = 0
    # F8: unit-economics visibility (the cost lever). None if analytics unavailable.
    e2c: Decimal | None = None
    cost_drag_pct: Decimal | None = None
    gross_expectancy: Decimal | None = None


def competition_markets() -> list[str]:
    """Markets that have at least one competitor wallet (drives the dashboard
    market selector). IN first, then the rest — always non-empty."""
    with conn() as c:
        found = {r["market"] for r in c.execute(
            "SELECT DISTINCT market FROM competitor_wallets")}
    ordered = (["IN"] if "IN" in found else []) + sorted(found - {"IN"})
    return ordered or ["IN"]


def _book_filter(competitor_id: str) -> tuple[str, tuple]:
    """SQL fragment + params selecting one competitor's paper_trades.

    House rows are (NULL OR 'house-claude'); everyone else is an exact id.
    """
    if competitor_id == HOUSE_COMPETITOR_ID:
        return HOUSE_TRADE_FILTER, ()
    return "competitor_id = %s", (competitor_id,)


def _stats(c, competitor_id: str, market: str = "IN") -> dict:
    where, params = _book_filter(competitor_id)
    return c.execute(
        f"""
        SELECT
          COALESCE(SUM(COALESCE(net_pnl_inr, pnl_inr))
                   FILTER (WHERE status = 'CLOSED'), 0)            AS realised,
          COALESCE(SUM(qty * entry_price)
                   FILTER (WHERE status = 'OPEN'), 0)              AS locked,
          COUNT(*) FILTER (WHERE status = 'OPEN')                  AS open_n,
          COUNT(*) FILTER (WHERE status = 'CLOSED')                AS trades,
          COUNT(*) FILTER (WHERE status = 'CLOSED'
                           AND COALESCE(net_pnl_inr, pnl_inr) > 0) AS wins,
          COUNT(*) FILTER (WHERE status = 'CLOSED'
                           AND COALESCE(net_pnl_inr, pnl_inr) < 0) AS losses
        FROM paper_trades
        WHERE {where} AND market = %s
        """,
        (*params, market),
    ).fetchone()


def leaderboard(market: str = "IN") -> list[LeaderRow]:
    """Competitors with a wallet in `market`, as ranked LeaderRows (highest equity
    first). Scoped to one market because equity in different currencies (₹ vs $)
    can't share a ranking. IN is the default (byte-identical to the prior board)."""
    rows: list[LeaderRow] = []
    with conn() as c:
        competitors = list(c.execute(
            """
            SELECT comp.id, comp.name, comp.backend, comp.model,
                   comp.autonomy_level, comp.status,
                   w.initial_capital_inr, w.currency
            FROM competitors comp
            JOIN competitor_wallets w
              ON w.competitor_id = comp.id AND w.market = %s
            ORDER BY comp.id
            """,
            (market,),
        ))
        for r in competitors:
            initial = Decimal(r["initial_capital_inr"]) if r["initial_capital_inr"] is not None \
                else Decimal("0")
            s = _stats(c, r["id"], market)
            realised = Decimal(s["realised"])
            locked = Decimal(s["locked"])
            equity = initial + realised
            available = equity - locked
            trades = int(s["trades"])
            wins = int(s["wins"])
            win_rate = (wins / trades * 100) if trades else None
            if initial > 0:
                pct = float((equity - initial) / initial * 100)
            else:
                pct = 0.0
            try:
                econ = book_economics(competitor_filter=r["id"])
                e2c, drag, gexp = econ.realised_e2c, econ.cost_drag_pct, econ.gross_expectancy
            except Exception:
                e2c = drag = gexp = None
            rows.append(LeaderRow(
                competitor_id=r["id"], name=r["name"] or r["id"],
                backend=r["backend"] or "", model=r["model"],
                autonomy_level=r["autonomy_level"] or "", status=r["status"] or "",
                market=market, currency=r["currency"] or "INR",
                initial=initial, realised_net_pnl=realised, equity=equity,
                locked=locked, available=available, open_positions=int(s["open_n"]),
                trades=trades, wins=wins, losses=int(s["losses"]),
                win_rate_pct=win_rate, progress_pct=pct,
                drawdown_pct=min(0.0, pct),
                e2c=e2c, cost_drag_pct=drag, gross_expectancy=gexp,
            ))

    rows.sort(key=lambda x: x.equity, reverse=True)
    for i, row in enumerate(rows, start=1):
        row.rank = i
    return rows

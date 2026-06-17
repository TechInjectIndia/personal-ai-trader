"""
Fund-only-winners leaderboard (S3).

Ranks every **agent × market × strategy** combination by its realised paper-trade
evidence, so the operator can SEE which combinations are productive — the
concrete surface behind the North Star's "fund only long-term winners". This
complements the per-market M8 gate (`go_live_readiness`): the gate is the binary
"is this market fundable?" verdict; the leaderboard is the ranked *which combo*
view that feeds it.

Pure SQL aggregation over `paper_trades` (joined to its strategy) → deterministic
given the data (no LLM, no RNG). `now()` only sets the look-back window boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from helm.data.store import conn

_TWO = Decimal("0.01")


@dataclass(frozen=True)
class ComboScore:
    market: str
    currency: str                   # venue currency (INR/USD) — net/expectancy are in this
    competitor_id: str | None       # None / 'house-claude' = house book
    strategy: str | None
    n: int
    net: Decimal                    # Σ net P&L (venue currency)
    expectancy: Decimal             # net / n — the ranking key
    win_pct: Decimal
    cost_drag: Decimal              # charges / |gross|
    score: Decimal                  # composite ranking key (= expectancy)

    def label(self) -> str:
        agent = self.competitor_id or "house"
        return f"{self.market}:{agent}:{self.strategy or '—'}"


def combo_scores(days: int = 30, min_trades: int = 1) -> list[ComboScore]:
    """All (market, competitor, strategy) combos with ≥ min_trades closed trades
    in the window, ranked by net expectancy/trade (desc), then volume."""
    with conn() as c:
        rows = list(c.execute(
            """
            SELECT pt.market AS market, pt.competitor_id AS competitor_id,
                   s.strategy AS strategy,
                   count(*) AS n,
                   COALESCE(SUM(pt.net_pnl_inr), 0) AS net,
                   COALESCE(SUM(pt.pnl_inr), 0)     AS gross,
                   COALESCE(SUM(pt.charges_inr), 0) AS charges,
                   count(*) FILTER (WHERE pt.net_pnl_inr > 0) AS wins
            FROM paper_trades pt
            LEFT JOIN decisions d ON d.id = pt.decision_id
            LEFT JOIN signals s ON s.id = d.signal_id
            WHERE pt.status = 'CLOSED' AND pt.net_pnl_inr IS NOT NULL
              AND pt.exit_ts >= now() - make_interval(days => %s)
            GROUP BY pt.market, pt.competitor_id, s.strategy
            HAVING count(*) >= %s
            """,
            (days, min_trades),
        ))
    from helm.markets import get_market

    def _ccy(mkt: str) -> str:
        try:
            return get_market(mkt).currency
        except KeyError:
            return ""

    out: list[ComboScore] = []
    for r in rows:
        n = int(r["n"])
        net, gross, charges = Decimal(r["net"]), Decimal(r["gross"]), Decimal(r["charges"])
        wins = int(r["wins"])
        expectancy = (net / n) if n else Decimal("0")
        win_pct = (Decimal(wins) / n * 100) if n else Decimal("0")
        cost_drag = (charges / abs(gross)) if gross != 0 else Decimal("0")
        out.append(ComboScore(
            market=r["market"], currency=_ccy(r["market"]),
            competitor_id=r["competitor_id"], strategy=r["strategy"],
            n=n, net=net.quantize(_TWO), expectancy=expectancy.quantize(_TWO),
            win_pct=win_pct.quantize(_TWO), cost_drag=cost_drag.quantize(_TWO),
            score=expectancy.quantize(_TWO),
        ))
    # Deterministic order: score desc, then volume desc, then stable label.
    out.sort(key=lambda c: (c.score, c.n, c.label()), reverse=True)
    return out


def fund_candidates(days: int = 30, min_trades: int = 30,
                    min_expectancy: Decimal = Decimal("0")) -> list[ComboScore]:
    """Combos that have *earned* consideration for real capital: enough trades
    AND positive net expectancy. The filter is SIGN-based (expectancy > 0), so it
    is currency-agnostic — a USD and an INR combo are each judged against zero, not
    against each other (cross-currency magnitude is NOT comparable; the `currency`
    field disambiguates display). The human still funds manually, per market."""
    return [c for c in combo_scores(days, min_trades) if c.expectancy > min_expectancy]


__all__ = ["ComboScore", "combo_scores", "fund_candidates"]

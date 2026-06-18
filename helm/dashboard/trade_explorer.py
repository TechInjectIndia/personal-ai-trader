"""
Cross-market, cross-agent trade explorer (Markets page).

Pure query/transform helpers so the filters + sorting are unit-testable without
Streamlit or a live DB. Agent attribution lives on `paper_trades.competitor_id`:
NULL or 'house-claude' is the incumbent house bot, every other value is a
competition agent (see HOUSE_TRADE_FILTER in config). Strategy is reached by
joining decisions → signals (paper_trades has no strategy column).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Sentinel filter values (kept out of the competitor_id namespace).
ALL = "__all__"
HOUSE = "__house__"

# Time-frame label → lookback days (0 = today's IST session, None = all time).
PERIODS: dict[str, int | None] = {
    "Today": 0,
    "Last 7 days": 7,
    "Last 30 days": 30,
    "All time": None,
}

# Sort label → the (computed) column the page sorts on. Descending is the
# sensible default for all three (best %/biggest win/newest first).
SORTS: dict[str, str] = {
    "% Net P&L": "pct",
    "Net P&L": "net",
    "Most recent": "ts",
}

# House = no competitor_id, or the legacy 'house-claude' tag. Qualified to the
# `pt` alias because the explorer query joins decisions/signals (which also carry
# a competitor_id, so a bare column name would be ambiguous). Mirrors
# config.HOUSE_TRADE_FILTER.
_HOUSE_CLAUSE = "(pt.competitor_id IS NULL OR pt.competitor_id = 'house-claude')"

_BASE_SQL = """
    SELECT pt.exit_ts, pt.market, pt.symbol, pt.side, pt.qty, pt.entry_price,
           pt.exit_price, pt.net_pnl_inr AS net, pt.exit_reason,
           pt.competitor_id, s.strategy
    FROM paper_trades pt
    LEFT JOIN decisions d ON d.id = pt.decision_id
    LEFT JOIN signals   s ON s.id = d.signal_id
    WHERE {where}
    ORDER BY pt.exit_ts DESC
    LIMIT %s
"""


def period_cutoff(label: str, now: datetime) -> datetime | None:
    """Lower bound on exit_ts (IST) for the chosen period; None = unbounded."""
    days = PERIODS.get(label)
    if days is None:
        return None
    local = now.astimezone(IST)
    if days == 0:
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local - timedelta(days=days)


def pct_net(net, entry, qty) -> float | None:
    """Net P&L as % of entry notional (entry_price × qty). None if no notional
    (missing/zero price or qty) so it can't divide-by-zero or fake a 0%."""
    try:
        notional = float(entry) * float(qty)
        if not notional:
            return None
        return float(net) / notional * 100.0
    except (TypeError, ValueError):
        return None


def filter_clauses(agent: str, market: str, cutoff: datetime | None) -> tuple[list[str], list]:
    """WHERE fragments + params for the explorer filters. `agent` ∈ {ALL, HOUSE,
    <competitor_id>}; `market` ∈ {ALL, <market key>}; `cutoff` bounds exit_ts."""
    clauses: list[str] = ["pt.status = 'CLOSED'"]
    params: list = []
    if agent == HOUSE:
        clauses.append(_HOUSE_CLAUSE)
    elif agent != ALL:
        clauses.append("pt.competitor_id = %s")
        params.append(agent)
    if market != ALL:
        clauses.append("pt.market = %s")
        params.append(market)
    if cutoff is not None:
        clauses.append("pt.exit_ts >= %s")
        params.append(cutoff)
    return clauses, params


def query_trades(cur, agent: str, market: str, cutoff: datetime | None,
                 limit: int = 2000) -> list[dict]:
    """Run the explorer query against an open cursor (dict_row). Pure-ish: no
    connection management here so callers control the transaction."""
    clauses, params = filter_clauses(agent, market, cutoff)
    sql = _BASE_SQL.format(where=" AND ".join(clauses))
    return list(cur.execute(sql, (*params, limit)).fetchall())


def agent_options(cur) -> list[tuple[str, str]]:
    """(value, label) options for the agent filter: All, House, then every
    competition agent that has actually traded, labelled by competitors.name."""
    rows = cur.execute(
        "SELECT DISTINCT pt.competitor_id AS id, c.name AS name "
        "FROM paper_trades pt LEFT JOIN competitors c ON c.id = pt.competitor_id "
        "WHERE pt.competitor_id IS NOT NULL AND pt.competitor_id <> 'house-claude' "
        "ORDER BY 1"
    ).fetchall()
    opts = [(ALL, "All agents"), (HOUSE, "House")]
    opts += [(r["id"], r["name"] or r["id"]) for r in rows]
    return opts


def agent_label(competitor_id: str | None, names: dict[str, str]) -> str:
    """Display label for a trade's agent. House for NULL/'house-claude'."""
    if competitor_id in (None, "house-claude"):
        return "House"
    return names.get(competitor_id, competitor_id)

"""Competitor records + week helpers shared across the league modules.

Kept dependency-light (store + stdlib only) so both the per-cycle runner and the
weekly mandate planner can import it without an import cycle. `helm.competition.
runner` re-exports `Competitor`, `freestyle_competitors`, and `get_competitor`
for backwards compatibility with existing callers (scripts/run_competitors.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helm.data.store import conn

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Competitor:
    id: str
    name: str
    backend: str
    model: str | None
    persona: str
    autonomy_level: str
    status: str


_SELECT = (
    "SELECT id, name, backend, model, persona, autonomy_level, status FROM competitors"
)


def freestyle_competitors() -> list[Competitor]:
    """Active freestyle competitors, in stable id order."""
    with conn() as c:
        rows = list(c.execute(
            f"{_SELECT} WHERE status = 'active' AND autonomy_level = 'freestyle' "
            "ORDER BY id ASC"
        ))
    return [Competitor(**r) for r in rows]


def get_competitor(competitor_id: str) -> Competitor | None:
    with conn() as c:
        row = c.execute(f"{_SELECT} WHERE id = %s", (competitor_id,)).fetchone()
    return Competitor(**row) if row else None


def week_start(d: date | None = None) -> date:
    """Monday of the week containing `d` (defaults to today IST).

    Mandates are keyed by (competitor_id, week_start) where week_start is the
    Monday of the trading week.
    """
    d = d or datetime.now(IST).date()
    return d - timedelta(days=d.weekday())

"""
Pre-market intraday gate.

Runs at 09:00 IST. If all checks pass, intraday strategies are armed for
the 09:15 open. If any fails, intraday is disabled for the day; passive
sleeve is unaffected.

PRD reference: §11.2 intraday-pre-market job, §13 intraday-lab discipline.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class GateResult:
    armed: bool
    failures: list[str]


def run_intraday_gate(
    total_nav_inr: Decimal,
    sleeve_b_cash_inr: Decimal,
    open_mis_positions: int,
    india_vix: Decimal,
    backtests_max_age_days: int,
    consecutive_losing_days: int,
) -> GateResult:
    """TODO: implement Phase 7. Skeleton checks:
      - sleeve_b_cash_inr / total_nav_inr within sleeve B cap
      - open_mis_positions == 0 (no overnight residual)
      - india_vix < 28 (else defensive mode)
      - backtests_max_age_days < 35
      - consecutive_losing_days < 3
    """
    raise NotImplementedError("intraday_gate.run_intraday_gate — Phase 7")

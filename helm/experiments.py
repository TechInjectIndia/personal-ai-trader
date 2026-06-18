"""
Autonomous flag-experiment decision logic (#4) — pure functions, unit-tested.

The controller (scripts/experiment_controller.py) handles DB + flag flips; the
KEEP/REVERT call lives here so it's deterministic and testable without a DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ArmStats:
    """One arm (ON or OFF) of an experiment: closed-trade count + net P&L."""
    n: int
    net: Decimal

    @property
    def expectancy(self) -> Decimal:
        return (self.net / self.n) if self.n else Decimal("0")


def decide_ab(on: ArmStats, off: ArmStats) -> str:
    """Verdict for a completed ON/OFF experiment.

    Conservative-by-default: an experimental flag must EARN staying on by beating
    the OFF baseline on net expectancy/trade. If either arm produced no trades
    (no evidence), or ON merely ties/loses, revert to OFF (the simpler state).
    Returns 'keep_on' | 'revert_off'.
    """
    if on.n == 0 or off.n == 0:
        return "revert_off"
    return "keep_on" if on.expectancy > off.expectancy else "revert_off"


__all__ = ["ArmStats", "decide_ab"]

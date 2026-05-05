"""
Drift detector — passive sleeve only.

Pulls current holdings (tagged by sleeve), compares against target weights,
emits a rebalance signal when any sleeve component drifts > 5%.

PRD reference: §11.2 drift-check job (weekly Sun 18:00 IST), §13 drift threshold.
"""

from dataclasses import dataclass
from decimal import Decimal


DEFAULT_DRIFT_THRESHOLD = Decimal("0.05")


@dataclass
class DriftSignal:
    symbol: str
    current_weight: Decimal
    target_weight: Decimal
    drift_abs: Decimal
    direction: str        # "buy" if underweight, "sell" if overweight


def detect_drift(
    current_weights: dict[str, Decimal],
    target_weights: dict[str, Decimal],
    threshold: Decimal = DEFAULT_DRIFT_THRESHOLD,
) -> list[DriftSignal]:
    """TODO: implement Phase 3."""
    raise NotImplementedError("drift_detector.detect_drift — Phase 3")

"""Shared move-sizing helpers for strategies (F4 bigger-move reframe).

Kept out of base.py so that module stays a pure ABC + dataclass with zero
numeric logic. Strategies import enforce_min_move() and apply it to the LOCAL
`target` value just before constructing a Signal — never mutate the constructed
object.
"""

from __future__ import annotations

from decimal import Decimal


def enforce_min_move(entry: Decimal, target: Decimal, side: str, min_pct: Decimal) -> Decimal:
    """Widen `target` so it sits at least `min_pct` away from `entry`.

    For a long (BUY) the floor is entry * (1 + min_pct); the returned target is
    never below that floor. For a short (SELL) the ceiling is entry * (1 - min_pct)
    and the returned target is never above it. A target already past the floor is
    returned unchanged. All paper-v1 strategies are long-only; the SELL branch is
    future-proofing.
    """
    if side == "BUY":
        floor = entry * (Decimal(1) + min_pct)
        return max(target, floor)
    ceiling = entry * (Decimal(1) - min_pct)
    return min(target, ceiling)

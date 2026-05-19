"""
Opening Range Breakout (ORB).

Rule:
  1. Define the opening range = high/low across the first `or_minutes` bars
     (15 minutes for the canonical setup, 5 minutes for the fast variant).
  2. After `or_minutes` has elapsed, on the first 1-min bar that closes ABOVE
     the opening high, emit a BUY signal. (Symmetric SHORT rule disabled for
     paper-mode v1 — Zerodha intraday-shorting works but we stay long-only
     while we tune.)
  3. Stop-loss = opening-range low. Target = entry + 1.5 × OR width.

Two instances are registered: `orb_15m` (canonical, slower, higher quality)
and `orb_5m` (faster trigger, lifts daily signal frequency — pairs with the
retros' "late-entry" tag count of 10).
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from helm.strategies.base import Signal, Strategy

MARKET_OPEN = time(9, 15)
RR_MULTIPLIER = Decimal("1.5")


class OpeningRangeBreakout(Strategy):
    def __init__(self, or_minutes: int = 15) -> None:
        self.or_minutes = or_minutes
        self._name = f"orb_{or_minutes}m"

    @property
    def name(self) -> str:
        return self._name

    def scan(self, symbol: str, candles: list[dict]) -> Signal | None:
        if len(candles) < self.or_minutes + 1:
            return None

        opening = candles[:self.or_minutes]
        or_high = max(Decimal(str(c["high"])) for c in opening)
        or_low = min(Decimal(str(c["low"])) for c in opening)
        or_width = or_high - or_low
        if or_width <= 0:
            return None

        # Only consider the most recent closed bar — strategies fire once at
        # the moment of breakout, not on every later bar.
        latest = candles[-1]
        latest_close = Decimal(str(latest["close"]))

        if latest_close <= or_high:
            return None

        # The bar BEFORE this one must NOT have closed above the high — this
        # ensures we fire on the breakout bar only, not on every bar after.
        prior = candles[-2]
        if Decimal(str(prior["close"])) > or_high:
            return None

        entry = latest_close
        stop = or_low
        target = entry + RR_MULTIPLIER * or_width

        return Signal(
            strategy=self.name,
            symbol=symbol,
            asof=latest["bar_ts"],
            side="BUY",
            entry_price=entry,
            stop_loss=stop,
            target=target,
            rationale=(
                f"ORB-{self.or_minutes}m: opening range {or_low}–{or_high} "
                f"(width {or_width:.2f}). Bar at {latest['bar_ts']} closed at "
                f"{entry} > range high. Stop {stop} (range low). "
                f"Target {target:.2f} ({RR_MULTIPLIER}× range)."
            ),
            payload={
                "or_minutes": self.or_minutes,
                "or_high": str(or_high),
                "or_low": str(or_low),
                "or_width": str(or_width),
                "rr_multiplier": str(RR_MULTIPLIER),
            },
        )

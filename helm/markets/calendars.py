"""
Per-venue trading calendars (FRD M1 wires NSE; M3 adds NYSE + 24/7 crypto).

NSECalendar lifts the exact weekday + window logic the live cron used
(`poll_market._is_market_open`, `scan_signals._within_trading_window`,
`manage_positions` square-off), so routing the IN path through it is
byte-identical. It deliberately does NOT check NSE holidays — the live code
never did, and adding holiday gating here would change behaviour.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from helm.config import (
    MARKET_CLOSE,
    MARKET_OPEN,
    SQUARE_OFF_AT,
    TRADING_END,
    TRADING_START,
)

IST = ZoneInfo("Asia/Kolkata")


class NSECalendar:
    """NSE equities: Mon–Fri, 09:15–15:30 IST, square-off 15:15 IST."""

    tz = IST

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _local(self, now: datetime | None) -> datetime:
        return (now or self.now()).astimezone(self.tz)

    def is_market_open(self, now: datetime | None = None) -> bool:
        n = self._local(now)
        if n.weekday() >= 5:
            return False
        return MARKET_OPEN <= n.time() <= MARKET_CLOSE

    def is_trading_window(self, now: datetime | None = None) -> bool:
        n = self._local(now)
        if n.weekday() >= 5:
            return False
        return TRADING_START <= n.time() <= TRADING_END

    def square_off_at(self) -> time | None:
        return SQUARE_OFF_AT

    def trading_day_key(self, ts: datetime) -> date:
        return ts.astimezone(self.tz).date()

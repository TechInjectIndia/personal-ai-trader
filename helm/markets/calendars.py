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
    tz_label = "IST"

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def session_hours(self) -> tuple[time, time] | None:
        return MARKET_OPEN, MARKET_CLOSE

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

    def resample_anchor_minutes(self) -> int:
        # N-min bars anchor to the session open (09:15 IST → 555).
        return MARKET_OPEN.hour * 60 + MARKET_OPEN.minute

    def trading_day_key(self, ts: datetime) -> date:
        return ts.astimezone(self.tz).date()


class AlwaysOpen:
    """24/7 venue (crypto): always open, no entry-window restriction, and NO EOD
    square-off (a position is closed by stop/target/time-stop, never a flatten).
    The 'today' boundary is the UTC date (daily-loss reset at 00:00 UTC)."""

    tz = ZoneInfo("UTC")
    tz_label = "UTC"

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def session_hours(self) -> tuple[time, time] | None:
        return None   # 24/7 — no regular session window

    def is_market_open(self, now: datetime | None = None) -> bool:
        return True

    def is_trading_window(self, now: datetime | None = None) -> bool:
        return True

    def square_off_at(self) -> time | None:
        return None

    def resample_anchor_minutes(self) -> int:
        return 0   # no session open; anchor N-min bars to UTC midnight

    def trading_day_key(self, ts: datetime) -> date:
        return ts.astimezone(self.tz).date()


class NYSECalendar:
    """US equities (NYSE/Nasdaq): ET regular session 09:30–16:00, with holiday +
    half-day awareness via pandas_market_calendars when installed. If the dep is
    missing it degrades to a weekday + fixed-hours gate (no holiday awareness)."""

    tz = ZoneInfo("America/New_York")
    tz_label = "ET"
    OPEN = time(9, 30)
    CLOSE = time(16, 0)
    ENTRY_END = time(15, 45)   # mirror IN: no new entries near the bell
    SQUARE_OFF = time(15, 55)

    def __init__(self) -> None:
        self._cal = None

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def session_hours(self) -> tuple[time, time] | None:
        return self.OPEN, self.CLOSE   # regular session (half-days handled live)

    def _local(self, now: datetime | None) -> datetime:
        return (now or self.now()).astimezone(self.tz)

    def _session(self, n: datetime) -> tuple[time, time] | None:
        """(open, close) ET for n's date, or None if the venue is closed that
        day. Uses the XNYS schedule (holidays/half-days); falls back to a
        weekday + fixed-hours gate if pandas_market_calendars is unavailable."""
        try:
            import pandas_market_calendars as mcal

            if self._cal is None:
                self._cal = mcal.get_calendar("XNYS")
            d = n.date().isoformat()
            sched = self._cal.schedule(start_date=d, end_date=d)
            if sched.empty:
                return None
            o = sched.iloc[0]["market_open"].tz_convert(self.tz).time()
            c = sched.iloc[0]["market_close"].tz_convert(self.tz).time()
            return o, c
        except Exception:
            if n.weekday() >= 5:
                return None
            return self.OPEN, self.CLOSE

    def is_market_open(self, now: datetime | None = None) -> bool:
        n = self._local(now)
        s = self._session(n)
        return bool(s and s[0] <= n.time() <= s[1])

    def is_trading_window(self, now: datetime | None = None) -> bool:
        n = self._local(now)
        s = self._session(n)
        if not s:
            return False
        # Cap entries at min(ENTRY_END, close) so half-days don't allow late entries.
        entry_end = min(self.ENTRY_END, s[1])
        return s[0] <= n.time() <= entry_end

    def square_off_at(self) -> time | None:
        return self.SQUARE_OFF

    def resample_anchor_minutes(self) -> int:
        return self.OPEN.hour * 60 + self.OPEN.minute   # 09:30 ET → 570

    def trading_day_key(self, ts: datetime) -> date:
        return ts.astimezone(self.tz).date()

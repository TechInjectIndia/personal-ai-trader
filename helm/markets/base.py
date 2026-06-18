"""
Market abstraction protocols + the `Market` record (FRD M1).

Protocols (duck-typed, no inheritance required) keep this a small, framework-free
seam — consistent with the "small single-user bot" rule. A concrete market wires
one DataAdapter + one Calendar + one CostModel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from helm.charges import ChargeBreakdown


@runtime_checkable
class DataAdapter(Protocol):
    """Reads prices for a venue. `last_price` powers the live poll; `historical`
    powers the backtester (M5). Both fail soft (return None / [] on error) so a
    data outage never crashes a cron tick."""

    def last_price(self, symbol: str) -> Decimal | None: ...

    def historical(
        self, symbol: str, bar_minutes: int, start: datetime, end: datetime
    ) -> list[dict]: ...


@runtime_checkable
class Calendar(Protocol):
    """When a venue trades + its square-off policy. `is_market_open` gates the
    poll/manage loops; `is_trading_window` gates new entries (scan/decide);
    `square_off_at` is the EOD flatten time (None ⇒ 24/7, no forced flatten);
    `trading_day_key` is the venue-local 'today' boundary; `session_hours` is the
    regular (open, close) for display (None ⇒ 24/7), labelled with `tz_label`."""

    tz: ZoneInfo
    tz_label: str   # short timezone label for display ("IST" | "ET" | "UTC")

    def now(self) -> datetime: ...

    def is_market_open(self, now: datetime | None = None) -> bool: ...

    def is_trading_window(self, now: datetime | None = None) -> bool: ...

    def square_off_at(self) -> time | None: ...

    def trading_day_key(self, ts: datetime) -> date: ...

    def session_hours(self) -> tuple[time, time] | None: ...


@runtime_checkable
class CostModel(Protocol):
    """Round-trip transaction cost for a closed trade. Same interface as
    `helm.charges` so the books and the cost-aware gates agree by construction.
    `qty` is Decimal-friendly so fractional (crypto) sizing works."""

    def round_trip_breakdown(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> ChargeBreakdown: ...

    def round_trip_charges(
        self, side: str, qty: Decimal, entry: Decimal, exit_price: Decimal
    ) -> Decimal: ...


@dataclass(frozen=True)
class Market:
    """One trading venue. Frozen — markets are configuration, not state."""

    key: str                       # "IN" | "US" | "CRYPTO"
    name: str                      # human label
    data: DataAdapter
    calendar: Calendar
    costs: CostModel
    currency: str                  # "INR" | "USD"
    fractional: bool               # False = integer lots; True = fractional qty
    watchlist: tuple[str, ...]     # symbols polled/scanned for this venue
    # Crypto-style venues have no daily close, so an open position can't be
    # flattened by a square-off. `max_hold_min` (set in M6) caps how long a
    # position may stay open as the 24/7 analog of EOD flat. None ⇒ the
    # calendar's square-off governs exits (sessioned venues: IN, US).
    max_hold_min: int | None = None

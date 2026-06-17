"""
Market abstraction (FRD M1).

A `Market` bundles everything that varies between trading venues — the data
adapter (how we read prices), the calendar (when the venue is open + its
square-off policy), the cost model (round-trip charges), the currency, and
whether positions are fractional. The incumbent NSE/Zerodha/IST/INR path is
`MARKET_IN`; US equities and crypto are added (disabled by default) in
M3/M6/M7. The cron scripts iterate `enabled_markets()` instead of hardcoding
NSE, so India stays byte-identical while new venues plug in behind the same
protocols.
"""

from helm.markets.base import Calendar, CostModel, DataAdapter, Market
from helm.markets.registry import (
    MARKET_IN,
    all_markets,
    enabled_markets,
    get_market,
)

__all__ = [
    "Market",
    "DataAdapter",
    "Calendar",
    "CostModel",
    "MARKET_IN",
    "all_markets",
    "enabled_markets",
    "get_market",
]

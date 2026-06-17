"""
Market registry (FRD M1).

Defines every known `Market` and which are enabled. `enabled_markets()` is what
the cron loops over; only those with `MARKET_ENABLED[key] is True` (config) run.
Default: IN only — US/CRYPTO are registered but disabled until their phases
(M6/M7) and the human flips the flag. Adding a venue is a registration here plus
a flag flip; no cron edits.
"""

from __future__ import annotations

from helm.config import (
    CRYPTO_EXCHANGE,
    CRYPTO_MAX_HOLD_MIN,
    CRYPTO_QUOTE,
    CRYPTO_TAKER_BPS,
    CRYPTO_WATCHLIST,
    MARKET_ENABLED,
    US_WATCHLIST,
    WATCHLIST,
)
from helm.markets.base import Market
from helm.markets.calendars import AlwaysOpen, NSECalendar, NYSECalendar
from helm.markets.costs import AlpacaEquityCosts, CryptoBpsCosts, ZerodhaCosts
from helm.markets.data import CCXTData, YFinanceNS, YFinanceUS

MARKET_IN = Market(
    key="IN",
    name="NSE Equities (India)",
    data=YFinanceNS(),
    calendar=NSECalendar(),
    costs=ZerodhaCosts(),
    currency="INR",
    fractional=False,
    watchlist=tuple(WATCHLIST),
)

MARKET_CRYPTO = Market(
    key="CRYPTO",
    name="Crypto (spot)",
    data=CCXTData(CRYPTO_EXCHANGE, CRYPTO_QUOTE),
    calendar=AlwaysOpen(),
    costs=CryptoBpsCosts(CRYPTO_TAKER_BPS),
    currency="USD",
    fractional=True,
    watchlist=tuple(CRYPTO_WATCHLIST),
    max_hold_min=CRYPTO_MAX_HOLD_MIN,
)

MARKET_US = Market(
    key="US",
    name="US Equities (NYSE/Nasdaq)",
    data=YFinanceUS(),
    calendar=NYSECalendar(),
    costs=AlpacaEquityCosts(),
    currency="USD",
    fractional=False,
    watchlist=tuple(US_WATCHLIST),
)

# All registered markets, keyed by Market.key. The dict is the single place a
# new venue is introduced. Enablement is separate (MARKET_ENABLED) so a venue
# can be registered but dark — IN is the only one enabled by default.
_ALL: dict[str, Market] = {
    MARKET_IN.key: MARKET_IN,
    MARKET_CRYPTO.key: MARKET_CRYPTO,
    MARKET_US.key: MARKET_US,
}


def _register(market: Market) -> None:
    """Add a market to the registry (used by M3/M6/M7 to extend _ALL)."""
    _ALL[market.key] = market


def all_markets() -> dict[str, Market]:
    """Every registered market (enabled or not), keyed by key."""
    return dict(_ALL)


def get_market(key: str) -> Market:
    """The Market for `key`; KeyError if unknown."""
    return _ALL[key]


def enabled_markets() -> list[Market]:
    """Markets the cron should run, in registration order. A market runs only
    when MARKET_ENABLED[key] is truthy; unknown/missing keys default to off."""
    return [m for k, m in _ALL.items() if MARKET_ENABLED.get(k, False)]

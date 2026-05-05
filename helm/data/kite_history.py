"""
Kite Connect /historical/ client.

Fetches historical OHLCV for Indian instruments using the API we're already
paying for. Caches to DuckDB to avoid repeated calls (Kite rate-limits and
charges-per-call don't apply, but we still cache for speed).

PRD reference: §11 data/kite_history.py.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd


CACHE_PATH = Path("helm_state/cache_in.duckdb")


def fetch_ohlcv(
    instrument_token: int,
    interval: str,        # 'minute' | '5minute' | 'day'
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """TODO Phase 2 — call kite.historical_data and cache to DuckDB."""
    raise NotImplementedError("data.kite_history.fetch_ohlcv — Phase 2")


def get_instrument_token(symbol: str, exchange: str = "NSE") -> int:
    """Look up the integer instrument token Kite uses for a tradingsymbol."""
    raise NotImplementedError("data.kite_history.get_instrument_token — Phase 2")

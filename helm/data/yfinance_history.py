"""
yfinance adapter — free historical OHLCV for US equities and ETFs.

Kite has no US data; yfinance is the free fallback. Daily bars only — sufficient
for the passive sleeve. The intraday sleeve runs only on Indian equities, so
US tick data is not required.

PRD reference: §11 data/yfinance_history.py.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd


CACHE_PATH = Path("helm_state/cache_us.duckdb")


def fetch_daily(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """TODO Phase 2 — yfinance.download() with caching to DuckDB."""
    raise NotImplementedError("data.yfinance_history.fetch_daily — Phase 2")

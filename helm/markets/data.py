"""
Per-venue data adapters (FRD M1 wires yfinance/.NS; M3 adds ccxt + Alpaca).

YFinanceNS lifts the exact live LTP call (`yf.Ticker(f"{sym}.NS").fast_info
.last_price`) from poll_market/manage_positions, with the same fail-soft
try/except, so routing IN through it is byte-identical. `yfinance` is imported
lazily inside the method so importing this module never pays the yfinance import
cost and the package stays importable even where a venue's optional dep is
absent (the M3 ccxt/Alpaca adapters follow the same lazy-import rule).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal


class YFinanceNS:
    """NSE equities via yfinance. Free, minutes-delayed — fine for paper."""

    suffix = ".NS"
    source = "yfinance"   # stamped into ticks.raw so the IN blob is unchanged

    def last_price(self, symbol: str) -> Decimal | None:
        import yfinance as yf

        try:
            return Decimal(str(yf.Ticker(f"{symbol}{self.suffix}").fast_info.last_price))
        except Exception:
            return None

    def historical(
        self, symbol: str, bar_minutes: int, start: datetime, end: datetime
    ) -> list[dict]:
        # Implemented in M5 (forward backtester) — yfinance intraday history is
        # capped at ~60 days, which the backtest report records as its window.
        raise NotImplementedError("YFinanceNS.historical lands with M5")

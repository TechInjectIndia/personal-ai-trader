"""
Tick poller — runs every minute via cron during NSE market hours (09:15-15:30 IST).

Pulls last-traded-price for each symbol in the watchlist via yfinance and writes
a row to `ticks`. Then folds recent ticks into 1-minute candles.

Why yfinance: the user's Kite Connect app does not have the market-data add-on,
so kite.ltp/quote/ohlc return PermissionException. yfinance is free, has NSE
coverage (with .NS suffix), and is delayed by minutes — fine for paper trading.
Swap this script for a kite.ticker WebSocket loop when the add-on is enabled.
"""

from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import yfinance as yf

from helm.config import MARKET_OPEN, MARKET_CLOSE, WATCHLIST
from helm.data.store import insert_audit, insert_tick, roll_minute_candles

IST = ZoneInfo("Asia/Kolkata")


def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:           # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def main() -> int:
    if not _is_market_open():
        # Cron will hit us off-hours too — no-op, no log spam.
        return 0

    now = datetime.now(IST)
    inserted = 0
    failed: list[str] = []
    for symbol in WATCHLIST:
        try:
            info = yf.Ticker(f"{symbol}.NS").fast_info
            ltp = Decimal(str(info.last_price))
            insert_tick(now, symbol, ltp, None, {"source": "yfinance"})
            inserted += 1
        except Exception as exc:
            failed.append(f"{symbol}: {exc}")

    candle_rows = roll_minute_candles()
    insert_audit(
        actor="poll_market",
        event="tick_poll",
        detail={
            "ts": now.isoformat(),
            "inserted": inserted,
            "failed": failed,
            "candle_upserts": candle_rows,
        },
    )

    if failed:
        print(f"poll_market: {inserted} ok, {len(failed)} failed: {failed}", file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

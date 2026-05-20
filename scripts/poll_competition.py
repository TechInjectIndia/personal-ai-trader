"""
Dynamic competition poller — runs every minute via cron during NSE market hours.

Polls last-traded-price (via yfinance) for the union of all active competitors'
current-week mandated symbols that fall OUTSIDE the house WATCHLIST, writes them
to `ticks`, then folds recent ticks into 1-minute candles. The incumbent
scripts/poll_market.py already covers WATCHLIST, so this script only adds the
*extra* symbols competitors chose — no double polling, and poll_market.py is
left completely untouched.

If no competitor has a mandate (or all mandated symbols are within WATCHLIST),
this script no-ops silently. Like the other cron scripts it also no-ops outside
market hours and on weekends.

NOT yet wired into crontab — going live is a human decision (mirrors
run_competitors.py). Intended cadence once enabled, alongside poll_market:

  * 3-9 * * 1-5  run_in_venv.sh scripts/poll_competition.py >> logs/poll_competition.log 2>&1

Why yfinance: the Kite app lacks the market-data add-on (kite.ltp/quote 403).
Same rationale as poll_market.py.
"""

from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yfinance as yf

from helm.competition.mandate import extra_polling_symbols
from helm.config import MARKET_CLOSE, MARKET_OPEN
from helm.data.store import insert_audit, insert_tick, roll_minute_candles

IST = ZoneInfo("Asia/Kolkata")


def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:           # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def main() -> int:
    if not _is_market_open():
        return 0

    symbols = extra_polling_symbols()
    if not symbols:
        # Nothing mandated beyond the house watchlist — poll_market covers it.
        return 0

    now = datetime.now(IST)
    inserted = 0
    failed: list[str] = []
    for symbol in symbols:
        try:
            info = yf.Ticker(f"{symbol}.NS").fast_info
            ltp = Decimal(str(info.last_price))
            insert_tick(now, symbol, ltp, None, {"source": "yfinance", "poller": "competition"})
            inserted += 1
        except Exception as exc:
            failed.append(f"{symbol}: {exc}")

    candle_rows = roll_minute_candles()
    insert_audit(
        actor="poll_competition",
        event="tick_poll",
        detail={
            "ts": now.isoformat(),
            "symbols": symbols,
            "inserted": inserted,
            "failed": failed,
            "candle_upserts": candle_rows,
        },
    )

    if failed:
        print(f"poll_competition: {inserted} ok, {len(failed)} failed: {failed}",
              file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

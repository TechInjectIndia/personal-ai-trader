"""
Tick poller — runs every minute via cron. No-ops outside every enabled market's
session (and on weekends), so cron firing off-hours is silent.

For each enabled market (helm.markets.enabled_markets) that is currently open,
pulls last-traded-price for each watchlist symbol via that market's data adapter
and writes a `ticks` row, then folds recent ticks into 1-minute candles.

The incumbent IN market uses the yfinance/.NS adapter (the user's Kite Connect
app lacks the market-data add-on, so kite.ltp/quote/ohlc 403). US/crypto adapters
plug in behind the same DataAdapter protocol (M3) and are disabled by default.
"""

from __future__ import annotations

import sys

from helm.data.store import insert_audit, insert_tick, roll_minute_candles
from helm.markets import enabled_markets


def main() -> int:
    inserted = 0
    failed: list[str] = []
    candle_upserts = 0
    ran_any = False

    for market in enabled_markets():
        if not market.calendar.is_market_open():
            # Each venue gates on its OWN calendar; cron fires 24/7 but only
            # open venues do work. No-op + no log spam outside sessions.
            continue
        ran_any = True
        now = market.calendar.now()
        source = getattr(market.data, "source", "market")
        for symbol in market.watchlist:
            ltp = market.data.last_price(symbol)
            if ltp is None:
                failed.append(f"{market.key}:{symbol}")
                continue
            insert_tick(now, symbol, ltp, None, {"source": source, "market": market.key},
                        market=market.key)
            inserted += 1
        candle_upserts += roll_minute_candles(market=market.key)

    if not ran_any:
        return 0

    insert_audit(
        actor="poll_market",
        event="tick_poll",
        detail={
            "inserted": inserted,
            "failed": failed,
            "candle_upserts": candle_upserts,
        },
    )

    if failed:
        print(f"poll_market: {inserted} ok, {len(failed)} failed: {failed}", file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

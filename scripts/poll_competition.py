"""
Dynamic competition poller — runs every minute via cron.

For each ENABLED market that's currently open, polls last-traded-price (via that
market's own data adapter) for the union of all active competitors' current-week
mandated symbols that fall OUTSIDE that market's house watchlist, writes them to
`ticks`, then folds recent ticks into 1-minute candles. The incumbent
poll_market.py already covers each market's house watchlist, so this script only
adds the *extra* symbols competitors chose — no double polling.

Each venue self-gates on its own calendar (NSE session, NYSE session, crypto
24/7), exactly like poll_market.py, so the single cron line works for all markets
and no-ops silently outside each market's hours.

  * * * * *  run_in_venv.sh scripts/poll_competition.py >> logs/poll_competition.log 2>&1

Why per-market adapters: IN uses yfinance .NS, US bare yfinance, crypto ccxt —
all behind market.data.last_price (Kite quotes 403 on this account).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.competition.mandate import extra_polling_symbols
from helm.data.store import insert_audit, insert_tick, roll_minute_candles
from helm.markets import enabled_markets


def main() -> int:
    inserted = 0
    failed: list[str] = []
    polled_markets: list[str] = []
    candle_upserts = 0

    for market in enabled_markets():
        if not market.calendar.is_market_open():
            continue
        symbols = extra_polling_symbols(market=market.key)
        if not symbols:
            # Nothing mandated beyond this market's house watchlist.
            continue
        polled_markets.append(market.key)
        now = market.calendar.now()
        source = getattr(market.data, "source", "market")
        for symbol in symbols:
            ltp = market.data.last_price(symbol)
            if ltp is None:
                failed.append(f"{market.key}:{symbol}")
                continue
            insert_tick(now, symbol, ltp, None,
                        {"source": source, "poller": "competition", "market": market.key},
                        market=market.key)
            inserted += 1
        candle_upserts += roll_minute_candles(market=market.key)

    if not polled_markets:
        return 0

    insert_audit(
        actor="poll_competition",
        event="tick_poll",
        detail={
            "markets": polled_markets,
            "inserted": inserted,
            "failed": failed,
            "candle_upserts": candle_upserts,
        },
    )
    if failed:
        print(f"poll_competition: {inserted} ok, {len(failed)} failed: {failed}",
              file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

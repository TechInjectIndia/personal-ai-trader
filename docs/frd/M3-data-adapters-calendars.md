# FRD M3 — Data Adapters + Calendars (per market)

_Multi-Market · Phase 1 · Owner: house engineer loop · Status: proposed · Depends on: M1, M2_

## 1. Problem
`poll_market.py` is hardwired to `yf.Ticker(f"{symbol}.NS").fast_info.last_price` and to NSE hours/weekday gating. US and crypto need different price sources, different historical sources (for M5), different sessions, and (crypto) no weekday/holiday gating at all.

## 2. Goal & success metrics
Provide one `DataAdapter` + one `Calendar` per market so the M1 cron loop is source-agnostic.
- Driver metric: each enabled market writes `candles_1m` rows during its own session and **no-ops** outside it (no log spam — preserves the cron convention).
- Outcome: `m.data.historical(...)` returns a uniform candle shape for all three markets (feeds M5).

## 3. HLD
Three adapter pairs behind the M1 protocols. Live LTP + historical bars share one shape: `{bar_ts, open, high, low, close, tick_count}` (matching `strategies/base.py` candle dicts).

```
IN     : YFinanceNS   (f"{sym}.NS")        + NSECalendar  (IST, 09:15–15:30, Mon–Fri, holidays)
US     : AlpacaData    or YFinanceUS (sym) + NYSECalendar (ET, 09:30–16:00, pandas_market_calendars)
CRYPTO : CCXTData      (binance, sym/USDT) + AlwaysOpen   (24/7, no square-off)
```

## 4. LLD
**`helm/markets/data/yfinance_ns.py`** — extracts today's `poll_market` call; `historical()` uses `yf.download(period, interval)` (≈60d of 1m max — documented limit).
**`helm/markets/data/ccxt_data.py`** — `ccxt.binance()` (no key for public data); `last_price` = `fetch_ticker(sym)["last"]`; `historical` = `fetch_ohlcv(sym, timeframe, since, limit)` paginated (ccxt caps ~1000 bars/call → loop). Symbol map e.g. `"BTC" → "BTC/USDT"`.
**`helm/markets/data/alpaca_data.py`** — REST `GET /v2/stocks/{sym}/bars` (free IEX feed) + `/trades/latest`; key/secret from `.env` (`ALPACA_KEY_ID`/`ALPACA_SECRET`), market-data base URL. yfinance US is the zero-key fallback.
**Calendars (`helm/markets/calendars.py`):**
```python
class NSECalendar:    # today's behavior, lifted from config.py + poll_market
    tz = ZoneInfo("Asia/Kolkata")
    def is_open(self, now): weekday<5 and MARKET_OPEN<=t<=MARKET_CLOSE
    def square_off_at(self): return SQUARE_OFF_AT
    def trading_day_key(self, ts): date_trunc IST day
class NYSECalendar:   # pandas_market_calendars 'XNYS' schedule; ET; half-days handled
class AlwaysOpen:     # is_open=>True; session_bounds=>None; square_off_at=>None; trading_day_key=>UTC date
```
**Dependencies:** add `ccxt`, `pandas_market_calendars`, `alpaca-py` (or raw `requests`) to `pyproject.toml`. ccxt + pmcal are pure-data, free.
**Rate/robustness:** every adapter fails soft (returns `None`/`[]`, logged to `audit`) exactly like the current yfinance try/except — a data outage must never crash the cron tick.

## 5. Acceptance
- `MARKET_IN` via `YFinanceNS` reproduces current `poll_market` rows (regression vs M1 baseline).
- `CCXTData.historical("BTC", 1, <90d range>)` returns ≥ ~129k contiguous 1-min bars, ascending, no dup `bar_ts`.
- `NYSECalendar.is_open` correctly returns False on a US market holiday and a half-day close.
- `AlwaysOpen.is_open` is True at 03:00 UTC Sunday (crypto trades weekends).

## 6. Out of scope / guardrails
No websockets (poll cadence stays cron-minute — the bot is delayed-data tolerant per the Kite-addon constraint). No paid data tiers in POC (yfinance/ccxt-public/Alpaca-IEX only). Crypto symbol universe and 24/7 session *handling* (square-off removal, daily-key) land in M6 — M3 only provides the adapter/calendar primitives.

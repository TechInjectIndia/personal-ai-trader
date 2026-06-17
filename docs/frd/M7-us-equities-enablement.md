# FRD M7 — US Equities Market Enablement (Alpaca / NYSE)

_Multi-Market · Phase 3 · Owner: house engineer loop · Status: proposed · Depends on: M1–M5_

## 1. Problem
US equities are the closest analog to the existing NSE flow (a sessioned, integer-share market) but on a different clock (ET, NYSE holidays/half-days), a different data source, a near-zero cost schedule, and — eventually — a different broker (Alpaca, whose paper account is free and first-class).

## 2. Goal & success metrics
Run a small US large-cap universe end-to-end: poll → candles → strategy → decide-off backtest → live paper, in a USD wallet, on the NYSE calendar.
- Driver metric: US `candles_1m` populate only during 09:30–16:00 ET on NYSE trading days (no rows on US holidays / half-day afternoons).
- Outcome: backtest + forward paper book exist for the US set, feeding M8.

## 3. HLD
Register `MARKET_US` with the M3 `AlpacaData` adapter (yfinance US fallback), `NYSECalendar`, `AlpacaEquityCosts` (M4), integer-share sizing (reuse the existing int path), a USD wallet, and an ET-anchored EOD square-off.

```
MARKET_US = Market("US", AlpacaData|YFinanceUS, NYSECalendar, AlpacaEquityCosts,
                   currency="USD", fractional=False)
square_off_at = 15:55 ET  → reuse manage_positions EOD flatten (sessioned, like IN)
trading_day_key = ET date
```

## 4. LLD
**Universe (`config.py`):** `US_WATCHLIST = ["AAPL","MSFT","NVDA","SPY","QQQ"]` (liquid large-caps + index ETFs — the US analog of the NIFTYBEES/BANKBEES ETF picks). yfinance/Alpaca use bare tickers (no `.NS`).
**Calendar:** `NYSECalendar` via `pandas_market_calendars` 'XNYS' — handles holidays and 13:00 ET half-day closes; `square_off_at` returns ~15:55 ET (mirrors the IN "flatten before the bell" intent). `trading_day_key` = ET date (the multi-market "today" idiom from M1).
**Sizing/exits:** integer shares — the existing `// entry_price` path works unchanged; the #681 breakeven + EOD time-decay levers apply directly (US is sessioned, like IN).
**Data:** prefer Alpaca IEX bars (free, key in `.env`); yfinance US is the zero-config fallback. Both yield the M3 candle shape.
**Cron:** the market loop (M1) fires the US path during ET hours via `NYSECalendar.is_open`. Add ET-windowed crontab lines (the bot host runs UTC; the calendar does the ET conversion, so crontab can poll across the ET session window in UTC and the calendar no-ops outside it).
**Strategies:** ORB/VWAP/gap-fade all assume a session open and **work as-is** on US (unlike crypto) — US gets the full `ACTIVE` set via the per-strategy `markets` allowlist (M6).

## 5. Acceptance
- US candles populate only inside NYSE sessions; zero rows on a US holiday and after a half-day close.
- A US paper trade flattens at the ET square-off (verified) and books net P&L using `AlpacaEquityCosts` (commission 0, SEC+TAF only).
- US P&L reports in USD in its own wallet; IN and CRYPTO books untouched.
- Backtest (M5) over the available US history (Alpaca/yfinance) produces a deterministic report; the report records the true window depth.

## 6. Out of scope / guardrails
**Paper only — no live US execution in this FRD** (live is M8-gated; Alpaca live needs an account + the funding decision). No options/extended-hours/PDT modeling — regular-session equities only. No fractional shares at launch (Alpaca supports them, but integer keeps parity with the existing path; revisit if small-notional sizing needs it). Free data tiers only (Alpaca IEX / yfinance) — no SIP/Polygon paid feeds in POC.

# FRD M6 — Crypto Market Enablement (24/7, fractional, ccxt)

_Multi-Market · Phase 2 · Owner: house engineer loop · Status: proposed · Depends on: M1–M5_

## 1. Problem
Crypto breaks the deepest live assumptions: there is **no daily close** (24/7), so the EOD square-off at 15:15 and the IST "today" boundary are meaningless; positions are **fractional**; and the universe/cost profile differ. With M1–M5 primitives in place, M6 turns crypto on as a real paper + backtest market.

## 2. Goal & success metrics
Run ≥2 liquid crypto pairs (BTC, ETH) end-to-end: poll → candles → strategy → decide-off backtest → live paper, in their own USD wallet.
- Driver metric: crypto `candles_1m` accumulate 24/7 (incl. weekends); crypto `paper_trades` open/close on stop/target with **no EOD flatten**.
- Outcome: a backtest + a forward paper book exist for BTC/ETH, feeding the M8 gate. **No INR/USD co-mingling.**

## 3. HLD
Register `MARKET_CRYPTO` with `CCXTData` + `AlwaysOpen` (M3), `CryptoBpsCosts` (M4), fractional sizing (M2), a USD wallet, and a daily-loss reset on a chosen rolling boundary.

```
MARKET_CRYPTO = Market("CRYPTO", CCXTData(binance), AlwaysOpen, CryptoBpsCosts,
                        currency="USD", fractional=True)
square_off_at = None  → manage_positions never force-closes; exits = stop/target/time-stop
trading_day_key = UTC date  → daily-loss kill-switch resets at 00:00 UTC
```

## 4. LLD
**Universe (`config.py`):** `CRYPTO_WATCHLIST = ["BTC","ETH"]` (+ ccxt symbol map `BTC→BTC/USDT`). Start tiny — liquidity + free data are best on majors.
**Sizing:** `paper_execute` fractional branch — `qty = (notional_cap / entry).quantize(step)`; per-venue lot/step precision from ccxt `market['precision']`. USD `max_position`/wallet in the `wallets` table (M2), separate kill-switch in `daily_state` keyed `(CRYPTO, utc_day)`.
**No square-off:** `manage_positions` already keys square-off on `m.calendar.square_off_at()` (M1) → `None` skips it. **But** a 24/7 book can't hold MIS-style intraday positions forever, so add a **max-hold time-stop** (config `CRYPTO_MAX_HOLD_MIN`, e.g. 240) that closes a stale open position — the crypto analog of EOD flat. Implemented in `manage_positions` for any market whose calendar has no square-off.
**Exit lock-in (#681) levers** (breakeven/time-decay) are session-relative — gate the time-decay tighten to markets *with* a square-off; crypto uses the time-stop instead. Breakeven-at-half-R still applies (session-agnostic).
**Strategies:** ORB/VWAP/gap-fade assume a session open (ORB = opening-range breakout). For 24/7, "session" is undefined → either (a) anchor ORB to UTC-day open, or (b) start crypto with session-agnostic strategies only (VWAP-reclaim, bbands z-score, mean-reversion). **Default: (b)** — enable only session-agnostic strategies for CRYPTO via a per-strategy `markets` allowlist; ORB-family stays IN/US until anchored.
**Cron:** `poll_market`/`scan_signals`/`manage_positions` already loop markets (M1); they now fire 24/7 for CRYPTO (the `AlwaysOpen` calendar gates them). Crypto cron must run on a UTC, weekend-inclusive schedule — add crypto-specific crontab lines (the IST-windowed ones stay for IN).

## 5. Acceptance
- BTC/ETH candles populate continuously incl. Saturday/Sunday; no gaps > a few minutes during normal operation.
- A crypto paper trade closes only on stop/target/time-stop — never an EOD flatten; verified across a UTC midnight.
- Crypto P&L reports in USD in its own wallet; the IN INR book is untouched (no cross-currency sum anywhere).
- Backtest (M5) of an enabled crypto strategy over ≥180d ccxt history produces a deterministic report.

## 6. Out of scope / guardrails
**Paper only — no live crypto execution in this FRD** (live is M8-gated and needs an exchange/regulatory decision: CoinDCX/WazirX for India vs offshore). No leverage/perps/funding-rate modeling — spot only. No new ORB anchoring (session-agnostic strategies only at launch). Honor the cost caution: crypto taker fees (~0.10% round-trip) exceed NSE drag, so M5 must show edge **survives** those fees before M8 considers funding.

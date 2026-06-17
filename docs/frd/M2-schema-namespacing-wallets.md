# FRD M2 — Schema: Market Namespacing, Per-Market Wallets, Fractional Qty

_Multi-Market · Phase 0 · Owner: house engineer loop · Status: proposed · Depends on: M1_

## 1. Problem
`symbol` is a bare string across `ticks`, `candles_1m`, `signals`, `paper_trades`, so `BTC` and `RELIANCE` would collide and P&L would co-mingle INR with USD. `qty` is an integer (crypto needs fractions). There is a single implicit INR wallet (`WalletConfig`), with no per-market accounting.

## 2. Goal & success metrics
Namespace all market data by `market`, give each market its own wallet/kill-switch, and allow fractional quantities — **without changing India's numbers**.
- Driver metric: India rows backfill to `market='IN'`; all existing house P&L/position queries return identical results.
- Outcome: a crypto and an equity book can coexist with zero P&L leakage between currencies.

## 3. HLD
Add a `market` column (default `'IN'`) to every market-scoped table, widen `qty` to `NUMERIC`, and model wallets per-market (mirrors the existing competitor book-isolation via `HOUSE_TRADE_FILTER`). All "today"/accounting queries gain a `market = %s` predicate.

```
ticks/candles_1m/signals/paper_trades  ── + market TEXT NOT NULL DEFAULT 'IN'
paper_trades.qty  INT ─► NUMERIC(18,8)   (int values unchanged for IN)
wallet            ── keyed by (market): INR pool for IN, USD pool for US/CRYPTO
daily_state       ── keyed by (market, trading_day)  (per-market kill-switch)
```

## 4. LLD
**Migration `scripts/migrate_multimarket.py` (idempotent, additive):**
```sql
ALTER TABLE ticks       ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'IN';
ALTER TABLE candles_1m  ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'IN';
ALTER TABLE signals     ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'IN';
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'IN';
ALTER TABLE paper_trades ALTER COLUMN qty TYPE NUMERIC(18,8);   -- int rows preserved exactly
-- candle uniqueness now per market:
DROP   INDEX IF EXISTS candles_1m_symbol_bar_ts_key;            -- (verify real name in schema.sql)
CREATE UNIQUE INDEX IF NOT EXISTS candles_1m_market_symbol_bar_ts
  ON candles_1m (market, symbol, bar_ts);
-- per-market wallet + per-market daily kill-switch:
CREATE TABLE IF NOT EXISTS wallets (
  market TEXT PRIMARY KEY,
  currency TEXT NOT NULL,
  initial_capital NUMERIC(18,2) NOT NULL,
  goal_capital   NUMERIC(18,2)
);
INSERT INTO wallets (market,currency,initial_capital,goal_capital)
  VALUES ('IN','INR',50000,100000) ON CONFLICT DO NOTHING;
ALTER TABLE daily_state ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'IN';
```
**`helm/wallet.py`:** functions take a `market` arg; `available(market)`, realised-P&L, and the daily-loss kill-switch all filter `WHERE market = %s AND HOUSE_TRADE_FILTER`. Currency-aware (no cross-currency sums).
**`helm/data/store.py`:** `insert_tick`/`roll_minute_candles`/candle reads/`insert_signal` carry `market`; `roll_minute_candles` upserts on `(market, symbol, bar_ts)`.
**`scripts/paper_execute.py`:** stamps `market` on the `paper_trades`/`decisions` rows; sizing uses fractional qty when `market.fractional` (else `// entry_price` int as today).
**`helm/config.py`:** `WALLET` stays the IN default; per-market capital lives in the `wallets` table (read via `live_wallet_config(market)`).

## 5. Acceptance
- Migration is idempotent (re-run = no-op) and **additive** (no India row rewritten beyond the `DEFAULT 'IN'` backfill).
- All existing house queries return identical pre/post numbers (regression-tested against a snapshot).
- A fractional `qty` (e.g. `0.0125` BTC) round-trips through `paper_trades` and P&L without truncation.
- Two wallets (`IN` INR, a test `CRYPTO` USD) report independent balances; no query sums across currencies.

## 6. Out of scope / guardrails
No change to the competitor `competitor_id` model — `market` is orthogonal (a competitor trades within one market). No FX conversion layer (each book is reported in its own currency; a converted "portfolio" view is a later dashboard-only concern). Keep `dict_row`/autocommit/`dbname=helm` — no pool/async.

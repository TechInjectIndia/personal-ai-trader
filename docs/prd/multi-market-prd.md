# Helm — Multi-Market Expansion PRD (India · US · Crypto)

_Owner: PM (Claude). Created 2026-06-17. Status: proposed (plan-only, no code)._
_North star: one engine trades & validates **three markets** (NSE equities, US equities, crypto) under paper + backtest; real capital is committed per-market **only** after sustained positive paper AND backtest._

---

## 1. Thesis — we are closer than a rewrite

Three of the hard pieces already exist and are market-agnostic at their core:

1. **Paper trading is internal, not broker-provided.** `scripts/paper_execute.execute_signal` → `helm.orchestrator.risk.evaluate` → `paper_trades` is a self-contained simulator. Kite is used **only** for live order placement + `profile()`/`margins()`. Adding a market for *paper* needs **data + a cost model + a calendar**, not a broker integration.
2. **A replay/eval engine already exists** — `helm/eval/{replay,backtest,metrics,gate}.py` + `scripts/replay_backtest.py`. `simulate_trade()` and `metrics.py` are reusable as-is; what's missing for a *true* backtest is a **historical feed** that runs strategies forward from history (today's backtest re-prices *recorded* trades for the eval-gate).
3. **Strategies are already pure functions** (`Strategy.scan(symbol, candles) -> Signal`, `helm/strategies/base.py`) with no market assumptions. They port for free.

So this is a **configuration / abstraction** program, not a rewrite. The work is to factor out everything currently hardcoded to NSE/Kite/IST/INR behind a `Market` and add per-market data, cost, calendar, and a forward backtester.

## 2. What is hardcoded to NSE today (the surface to refactor)

| Coupling | Location | Assumption |
|---|---|---|
| Symbols + `.NS`, `EXCHANGE="NSE"` | `config.py`, `scripts/poll_market.py` | NSE tickers via yfinance |
| Trading hours + EOD square-off 15:15 | `config.py`, `scripts/manage_positions.py` | A sessioned market with a daily close |
| IST "today" `date_trunc` idiom | everywhere in SQL | A single daily boundary |
| Cost model (STT/stamp/GST/SEBI) | `helm/charges.py` | Zerodha intraday equity |
| Currency = INR | every `_inr` Decimal, `WALLET` | One INR pool |
| `qty` is integer | `schema.sql`, sizing in `paper_execute` | Whole-share lots (breaks crypto fractions) |

## 3. Success metrics

- **Driver:** India trade path is **byte-identical** after the M1/M2 refactor (same decisions, same fills on a replay diff).
- **Coverage:** ≥1 strategy runs end-to-end (signal → decide-off backtest → paper) on **all three** markets.
- **Validation gate (the whole point):** a market is funded only when its per-market book shows **net expectancy/trade > 0 over a trailing 30-trade paper window AND a positive backtest** over the same strategy set (M8).
- **Cost discipline (POC):** $0 incremental spend for data + backtest; LLM decider **off** in backtests (raw edge measured deterministically).

## 4. Architecture — the `Market` abstraction

A `Market` is the single record that parameterizes everything in §2:

```
Market(
  key:        "IN" | "US" | "CRYPTO"
  data:       DataAdapter      # last_price(sym), historical(sym, tf, range)
  calendar:   Calendar         # is_open(now), session bounds, square_off policy|None, tz
  costs:      CostModel        # round_trip_breakdown(side, qty, entry, exit) -> ChargeBreakdown
  currency:   "INR" | "USD"
  sizing:     int-lots | fractional
  wallet:     per-market capital pool (never co-mingled)
)
```

The four cron scripts iterate over **enabled markets** instead of the single NSE path. India is "market `IN`" with today's exact adapters, so behavior is unchanged.

## 5. Integration choices (cost-minimized)

| | India (today) | US equities | Crypto |
|---|---|---|---|
| Paper data | yfinance `.NS` (free) | yfinance / Alpaca IEX (free) | **ccxt** → Binance public OHLCV (free, no key) |
| Backtest history | yfinance ~60d 1m (weak) | Alpaca historical (free) | **ccxt `fetch_ohlcv`** — years of 1m (best) |
| Live exec (later) | Kite | **Alpaca** ($0 comm, free paper API) | ccxt → CoinDCX/Binance |
| Cost model | STT/stamp/GST (exists) | SEC+TAF, ~$0 comm | maker/taker bps (~0.075–0.1%) |
| Calendar | NSE hrs + holidays | NYSE (`pandas_market_calendars`) | **24/7, no square-off** |
| Sizing | int lots | int shares | **fractional** |

**Winners:** **Alpaca** for US (Kite-equivalent, free first-class paper), **ccxt** for crypto (one lib, free unlimited history → best backtest dataset).

## 6. Roadmap — FRDs

| FRD | Theme | Phase | Exit gate |
|---|---|---|---|
| [M1](../frd/M1-market-abstraction.md) | `Market` interface + registry; India as market `IN` | 0 (refactor, dark) | India replay diff = 0 |
| [M2](../frd/M2-schema-namespacing-wallets.md) | Schema: market namespacing, per-market wallets, fractional qty | 0 | Migration idempotent; India accounting unchanged |
| [M3](../frd/M3-data-adapters-calendars.md) | `DataAdapter` + `Calendar` per market (yfinance/Alpaca/ccxt) | 1 | Each market fills `candles_1m` |
| [M4](../frd/M4-cost-models.md) | Per-market `CostModel` (US SEC+TAF, crypto bps) | 1 | Net P&L matches venue schedule |
| [M5](../frd/M5-historical-backtester.md) | True forward backtester (decider-off), reuses `simulate_trade`+`metrics` | 1 | Backtest reproducible & deterministic |
| [M6](../frd/M6-crypto-enablement.md) | Crypto market: 24/7 session, no square-off, fractional, ccxt | 2 | BTC/ETH paper + backtest live |
| [M7](../frd/M7-us-equities-enablement.md) | US market: Alpaca data, NYSE calendar | 3 | US large-caps paper + backtest live |
| [M8](../frd/M8-go-live-funding-gate.md) | Per-market go-live funding gate (paper∧backtest > 0) | 4 | No capital without passing gate |

## 7. Sequencing & rationale

**Crypto before US.** ccxt gives free, unlimited 1-min history (best backtest) and 24/7 means far more signal volume to validate strategies faster, with no live-execution/regulatory entanglement while paper-only. US (Alpaca) follows — free paper account, clean path to live.

**A blunt caution from live data (2026-06-17 digest):** the India book is still **net-negative** (freestyle −₹1,159/72 trades, 19% win) because a thin edge is eaten by cost. Crypto taker fees (~0.1% round-trip) are **higher** than the ~0.05% NSE drag, so the same scalping edge loses *harder* on crypto unless targets are materially bigger. M5's first job is to prove edge **survives crypto fees** before any capital — the M8 gate enforces it.

## 8. Out of scope / guardrails

- No change to how strategies generate signals or how the self-improvement loop runs.
- No co-mingling of INR/USD P&L — each market is its own wallet + kill-switch + dashboard view (reuse the `HOUSE_TRADE_FILTER` book-isolation pattern).
- No LLM decider in backtests (cost + determinism). LLM-in-the-loop is validated forward, in paper only.
- Live trading is gated behind M8; nothing here places a real order.

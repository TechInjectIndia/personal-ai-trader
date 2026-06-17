# FRD M4 — Per-Market Cost Models

_Multi-Market · Phase 1 · Owner: house engineer loop · Status: proposed · Depends on: M1_

## 1. Problem
`helm/charges.py` encodes the Zerodha NSE intraday schedule (STT, stamp duty, GST, SEBI, exchange txn). It is the *same* model the books and the F2 min-edge gate use, so net P&L mirrors live. US and crypto have entirely different fee structures; using the NSE model there would make net P&L and the F2 gate wrong for those markets.

## 2. Goal & success metrics
A `CostModel` protocol with one method matching today's signature, and three concrete implementations. The F2 gate and net-P&L stay correct per market by construction (same model used for gate and books).
- Driver metric: net P&L on a US/crypto paper trade equals a hand-computed venue-schedule figure.
- Outcome: `round_trip_breakdown` callers (`paper_execute`, `charges` consumers, `eval/replay`) become market-parameterized with no logic change.

## 3. HLD
Keep the exact `ChargeBreakdown`/`round_trip_breakdown(side, qty, entry, exit)` interface; resolve the implementation from `market.costs`. `ZerodhaCosts` simply wraps the existing function — India unchanged.

```
CostModel.round_trip_breakdown(side, qty, entry, exit) -> ChargeBreakdown
  IN     : ZerodhaCosts  -> existing helm/charges.py (verbatim)
  US      : AlpacaEquityCosts -> commission 0 + SEC fee (sell) + FINRA TAF (sell)
  CRYPTO  : CryptoBpsCosts     -> taker_bps on both legs (no STT/stamp/GST)
```

## 4. LLD
**`helm/markets/costs/base.py`** — the `CostModel` Protocol + re-export of `ChargeBreakdown`.
**`helm/markets/costs/zerodha.py`** — thin adapter delegating to `helm.charges.round_trip_breakdown` (single source of truth preserved; no copy).
**`helm/markets/costs/us_equity.py`:**
```python
_SEC_RATE = Decimal("0.0000278")   # SEC fee, sell-side notional (revisit; published rate)
_TAF_PER_SHARE = Decimal("0.000166")  # FINRA TAF per share sold, capped per order
# commission = 0 (Alpaca); GST/stamp/STT = 0. total = sec(sell) + taf(sell).
```
**`helm/markets/costs/crypto_bps.py`:**
```python
_TAKER_BPS = Decimal("0.0010")     # 0.10% Binance spot taker (config-tunable per venue)
# round trip = (buy_value + sell_value) * _TAKER_BPS. No tax components.
```
All return a populated `ChargeBreakdown` (unused components = 0) so existing readers/UI work unchanged.
**Config:** per-market rate constants live beside the model (code-is-config). Expose `MIN_EDGE_TO_COST` per market if needed later — for now the single tunable applies, and because crypto fees are higher the same gate is *stricter* there (desired).
**Wiring:** `paper_execute` and `helm/eval/replay.simulate_trade` take the cost model from the trade's `market` instead of importing `charges` directly.

## 5. Test plan
`tests/test_cost_models.py`: (a) `ZerodhaCosts` == `helm.charges` for a sample BUY and SELL (no drift); (b) US model: 100 shares @ \$200 sell → SEC+TAF matches hand calc, commission 0; (c) crypto: 0.01 BTC @ \$60k round-trip → `notional*0.001*2`; (d) `ChargeBreakdown` shape valid for all (unused fields 0, `total` = sum).

## 6. Rollout / revert
Pure addition; `IN` path delegates to today's code so production is unchanged until a non-IN market is enabled. Revert = drop the new models; `ZerodhaCosts` continues to work. Crypto/US rates are documented constants — update in one place when venues republish (same discipline as `charges.py`'s "rates as of 2026" note).

## 7. Risks
Stale published rates (mitigation: single documented constant per fee, dated). Per-order caps (US TAF, brokerage caps) modeled wrong (mitigation: unit tests pin the cap arithmetic, mirroring `charges._BROKERAGE_CAP`).

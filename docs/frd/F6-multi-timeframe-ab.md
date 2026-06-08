# FRD F6 — Multi-Timeframe A/B (5/15-min bars)

_Phase 2 · Owner: house engineer loop · Status: proposed · Depends on: F1, candles_1m_

## 1. Problem
On 1-min bars the tradeable move (~₹31) is tiny relative to the fixed ~₹13 cost, and 1-min noise drives the ~random entries. Higher timeframes have larger moves and cleaner structure vs the *same* fixed cost — a structurally better cost ratio.

## 2. Goal & success metrics
Run the existing strategies on aggregated 5-min (and optionally 15-min) bars as parallel variants and A/B vs 1-min.
- Driver: E2C and gross expectancy by timeframe (F1, sliced on strategy name suffix `_5m`/`_15m`).
- Outcome: identify the timeframe with the best gross expectancy × E2C; promote it.

## 3. HLD
Aggregate `candles_1m` → 5-min / 15-min bars and feed strategy variants the right series. Mirror the existing `orb_5m`/`orb_15m` naming so the loop and F1 attribute per timeframe.

```
candles_1m ──(resample by floor(bar_ts to 5/15 min): O=first, H=max, L=min, C=last)──► candles_Nm
scan_signals ──► for each (strategy, timeframe) variant: strategy.scan(symbol, candles_Nm)
```

## 4. LLD
**Aggregation** — add to `helm/data/store.py`:
```python
def resample_candles(symbol: str, minutes: int, lookback_bars: int) -> list[dict]:
    """Aggregate candles_1m into `minutes`-bars via date_bin (Postgres 14+):
       SELECT date_bin('%s min', bar_ts, <anchor>) gb, (array_agg(open ORDER BY bar_ts))[1] open,
              max(high) high, min(low) low, (array_agg(close ORDER BY bar_ts ORDER DESC))[1] close,
              sum(tick_count) tick_count FROM candles_1m WHERE symbol=%s GROUP BY gb ORDER BY gb."""
```
- Anchor `date_bin` to 09:15 IST so 5-min buckets align to the session open (09:15–09:20, …). Return the same dict shape strategies expect (`bar_ts, open, high, low, close, tick_count`) so **no strategy code changes**.
- Alternative (heavier): materialize `candles_5m`/`candles_15m` tables via a `roll_*` upsert; defer unless the on-the-fly aggregate is too slow (it won't be at this scale).

**Strategy variants** — strategies already accept a timeframe param (ORB takes `or_minutes`; bbands is "parametrizable for a fast variant"). Register timeframe-suffixed instances in `ACTIVE`, e.g. `bbands_zscore_5m`. Each variant's `name` carries the suffix → F1 attribution + the once-per-trigger-bar guard work per timeframe.

**`scan_signals.py`** — for a variant declared on timeframe N, pass `resample_candles(symbol, N, lookback)` instead of the raw 1-min list. Keep the `consumed`/dedupe and trigger-bar guards (they key on the variant's bar_ts, now N-min aligned).

## 5. Test plan
`tests/test_resample.py`: hand-built 1-min series → correct 5-min OHLC (first open, max high, min low, last close, summed ticks), session-anchored buckets, partial final bucket handling. Strategy-variant test: a `_5m` variant fires once per 5-min trigger bar and doesn't re-emit.

## 6. Rollout / revert
A/B alongside 1-min variants; compare in F1 over ≥2 weeks. Revert = remove the variants from `ACTIVE` (one-line). Aggregation helper is inert if unused.

## 7. Risks
Fewer bars/day → fewer signals → slower statistics (accept; quality over quantity is the point). `date_bin` anchor must match IST session open or buckets misalign — covered by tests.

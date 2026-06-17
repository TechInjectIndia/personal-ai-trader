# FRD M1 — Market Abstraction Layer + Registry

_Multi-Market · Phase 0 (dark refactor) · Owner: house engineer loop · Status: proposed · Depends on: nothing (pure refactor)_

## 1. Problem
Every market-specific assumption (NSE tickers, IST hours, EOD square-off, INR, Zerodha costs, int lots) is scattered across `config.py`, `poll_market.py`, `manage_positions.py`, `paper_execute.py`, and the SQL "today" idiom. There is no seam to add a second or third market without forking each script.

## 2. Goal & success metrics
Introduce one `Market` record that owns data/calendar/costs/currency/sizing, and make the cron path iterate over **enabled markets**. India becomes market `IN` with today's exact behavior.
- Driver metric: **India trade path is byte-identical** — a replay of the last 30 days produces the same `decisions`/`paper_trades` rows (diff = 0).
- Outcome: adding a market is a new `Market` registration + adapters, zero edits to the cron scripts.

## 3. HLD
A `helm/markets/` package defining the protocol and a registry. The cron scripts loop `for m in enabled_markets(): ...` where today's code runs as `MARKET_IN`.

```
helm/markets/
  base.py       # Market dataclass + DataAdapter/Calendar/CostModel protocols
  registry.py   # ENABLED: dict[str, Market]; enabled_markets()
  india.py      # MARKET_IN = Market("IN", YFinanceNS, NSECalendar, ZerodhaCosts, "INR", int-lots)
```

`Market` is a frozen dataclass; adapters are small protocol classes (Python `typing.Protocol`, no heavy framework — consistent with the "small single-user bot" rule).

## 4. LLD
**`helm/markets/base.py`:**
```python
class DataAdapter(Protocol):
    def last_price(self, symbol: str) -> Decimal | None: ...
    def historical(self, symbol: str, bar_minutes: int, start, end) -> list[dict]: ...

class Calendar(Protocol):
    tz: ZoneInfo
    def is_open(self, now) -> bool: ...
    def session_bounds(self, now) -> tuple[time, time] | None: ...   # None = 24/7
    def square_off_at(self) -> time | None: ...                       # None = no EOD flat
    def trading_day_key(self, ts) -> date: ...                        # the "today" boundary

@dataclass(frozen=True)
class Market:
    key: str                 # "IN" | "US" | "CRYPTO"
    data: DataAdapter
    calendar: Calendar
    costs: CostModel         # see M4; ZerodhaCosts wraps today's helm/charges.py
    currency: str            # "INR" | "USD"
    fractional: bool         # False=int lots, True=fractional qty (M2/M6)
```
**`helm/markets/registry.py`:** `ENABLED: dict[str, Market] = {"IN": MARKET_IN}`. `enabled_markets()` returns its values. Enabling US/crypto = adding a key (gated by a config flag per market, default only `IN`).

**`helm/markets/india.py`:** wires today's behavior verbatim — `YFinanceNS` adapter (the `f"{symbol}.NS"` call from `poll_market.py`), `NSECalendar` (MARKET_OPEN/CLOSE/SQUARE_OFF_AT from `config.py`, IST, weekday-only), `ZerodhaCosts` (adapter over existing `helm/charges.round_trip_breakdown`).

**Refactor (mechanical, no behavior change):**
- `poll_market.py`: `for m in enabled_markets(): for sym in m.watchlist: m.data.last_price(sym)`; `m.calendar.is_open(now)` replaces `_is_market_open`.
- `manage_positions.py`: EOD square-off keys on `m.calendar.square_off_at()` (None ⇒ skip — sets up M6).
- The SQL "today" idiom stays for `IN` but is sourced from `m.calendar.trading_day_key()` so crypto can override it (M6).

**Config:** `MARKET_ENABLED: dict[str,bool] = {"IN": True, "US": False, "CRYPTO": False}` in `config.py` (same code-is-config rule).

## 5. Test plan
`tests/test_markets.py`: (a) `MARKET_IN.calendar.is_open` matches old `_is_market_open` across a week of timestamps; (b) `ZerodhaCosts.round_trip` equals `helm/charges.round_trip_charges` for a sample; (c) registry returns only `IN` by default. **Replay diff harness:** run `scripts/replay_backtest.py` over 30d pre- and post-refactor; assert identical outcomes.

## 6. Rollout / revert
Pure refactor shipped dark — only `IN` enabled, so production is unchanged. Revert = git revert; no data migration in this FRD (that's M2).

## 7. Risks
A subtle behavioral drift in the IN wiring (mitigation: the byte-identical replay diff is the acceptance gate). Over-abstraction (mitigation: protocols only, no DI framework; one concrete market until M6/M7).

"""
Per-venue data adapters (FRD M1 wired yfinance/.NS; M3 adds ccxt + Alpaca).

Each adapter implements the DataAdapter protocol: `last_price` (live poll) and
`historical` (backtest feed, M5), both returning the canonical candle dict shape
`{bar_ts, open, high, low, close, tick_count}` that strategies already consume.
All network/3rd-party imports are LAZY (inside methods) so importing this module
is cheap and never hard-depends on an optional venue library; all calls fail
soft (None / []), mirroring the live yfinance try/except.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

UTC = timezone.utc


# --- shared helpers ---------------------------------------------------------
def _dec(x) -> Decimal:
    return Decimal(str(x))


def _candle(bar_ts: datetime, o, h, low, c, volume) -> dict:
    """Canonical candle dict. tick_count carries volume (int) as the liquidity
    proxy strategies expect; 0 when unknown."""
    try:
        tc = int(float(volume)) if volume is not None else 0
    except (TypeError, ValueError):
        tc = 0
    return {
        "bar_ts": bar_ts,
        "open": _dec(o),
        "high": _dec(h),
        "low": _dec(low),
        "close": _dec(c),
        "tick_count": tc,
    }


def _ccxt_timeframe(bar_minutes: int) -> str:
    if bar_minutes % 60 == 0 and bar_minutes >= 60:
        return f"{bar_minutes // 60}h"
    return f"{bar_minutes}m"


def _alpaca_timeframe(bar_minutes: int) -> str:
    if bar_minutes % 60 == 0 and bar_minutes >= 60:
        return f"{bar_minutes // 60}Hour"
    return f"{bar_minutes}Min"


def _yf_interval(bar_minutes: int) -> str:
    if bar_minutes >= 1440:
        return "1d"
    if bar_minutes % 60 == 0 and bar_minutes >= 60:
        return f"{bar_minutes // 60}h"
    return f"{bar_minutes}m"


def _parse_iso(s: str) -> datetime:
    """ISO8601 (incl. trailing 'Z') → tz-aware datetime (UTC if naive)."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _df_to_candles(df) -> list[dict]:
    """yfinance DataFrame → candle dicts (flattens single-ticker MultiIndex)."""
    import pandas as pd

    if df is None or len(df) == 0:
        return []

    def col(name: str):
        sel = None
        if name in df.columns:
            sel = df[name]                 # NOTE: MultiIndex level-0 match → DataFrame
        else:
            for c in df.columns:           # single-ticker MultiIndex: (field, ticker)
                if isinstance(c, tuple) and c[0] == name:
                    sel = df[c]
                    break
        if sel is None:
            return None
        # yfinance returns MultiIndex columns even for one ticker; `df['Close']`
        # then yields a 1-column DataFrame — squeeze it to a Series.
        if hasattr(sel, "columns"):
            sel = sel.iloc[:, 0]
        return sel

    o, h, low, c, v = (col("Open"), col("High"), col("Low"), col("Close"), col("Volume"))
    if any(x is None for x in (o, h, low, c)):
        return []
    out: list[dict] = []
    for i in range(len(df)):
        ts = df.index[i].to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        vol = v.iloc[i] if v is not None and not pd.isna(v.iloc[i]) else 0
        out.append(_candle(ts, o.iloc[i], h.iloc[i], low.iloc[i], c.iloc[i], vol))
    return out


# --- adapters ---------------------------------------------------------------
class YFinanceNS:
    """NSE equities via yfinance. Free, minutes-delayed — fine for paper.
    Intraday history is capped at ~60 days by yfinance (M5 records the window)."""

    suffix = ".NS"
    source = "yfinance"   # stamped into ticks.raw so the IN blob is unchanged

    def last_price(self, symbol: str) -> Decimal | None:
        import yfinance as yf

        try:
            return Decimal(str(yf.Ticker(f"{symbol}{self.suffix}").fast_info.last_price))
        except Exception:
            return None

    def historical(
        self, symbol: str, bar_minutes: int, start: datetime, end: datetime
    ) -> list[dict]:
        import yfinance as yf

        try:
            interval = _yf_interval(bar_minutes)
            # yfinance returns EMPTY for intraday with tz-aware start/end; the
            # period= form works. Clamp the period to yfinance's intraday history
            # caps (1m≈7d, other intraday≈60d, daily≈years).
            days = max(1, (end - start).days)
            cap = 7 if interval == "1m" else (60 if interval.endswith("m") else 730)
            df = yf.download(
                f"{symbol}{self.suffix}", period=f"{min(days, cap)}d",
                interval=interval, progress=False, auto_adjust=False,
            )
            return _df_to_candles(df)
        except Exception:
            return []


class YFinanceUS(YFinanceNS):
    """US equities via yfinance (bare ticker, no suffix). Zero-config free
    default for the US market; Alpaca (AlpacaData) is the keyed alternative for
    live + IEX bars. yfinance gives years of daily + ~60d of 1-min history."""

    suffix = ""
    source = "yfinance"


class CCXTData:
    """Crypto spot via ccxt PUBLIC endpoints (no API key for market data).
    Default venue binance; bare symbols map SYM -> SYM/{quote} (default USDT).
    ccxt OHLCV gives years of 1-min history — the strongest backtest feed."""

    source = "ccxt"

    def __init__(self, exchange: str = "binance", quote: str = "USDT") -> None:
        self.exchange_id = exchange
        self.quote = quote
        self._client = None

    def _ex(self):
        if self._client is None:
            import ccxt

            self._client = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        return self._client

    def _pair(self, symbol: str) -> str:
        return symbol if "/" in symbol else f"{symbol}/{self.quote}"

    def last_price(self, symbol: str) -> Decimal | None:
        try:
            last = self._ex().fetch_ticker(self._pair(symbol)).get("last")
            return _dec(last) if last is not None else None
        except Exception:
            return None

    def historical(
        self, symbol: str, bar_minutes: int, start: datetime, end: datetime
    ) -> list[dict]:
        try:
            ex = self._ex()
            tf = _ccxt_timeframe(bar_minutes)
            since = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            limit = 1000
            out: list[dict] = []
            while since < end_ms:
                batch = ex.fetch_ohlcv(self._pair(symbol), timeframe=tf, since=since, limit=limit)
                if not batch:
                    break
                for ts_ms, o, h, low, c, v in batch:
                    if ts_ms >= end_ms:
                        break
                    out.append(_candle(datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                                       o, h, low, c, v))
                last_ts = batch[-1][0]
                if last_ts < since or len(batch) < limit:   # no progress / exhausted
                    break
                since = last_ts + 1
            return out
        except Exception:
            return []


class AlpacaData:
    """US equities via Alpaca market-data REST (free IEX feed). Optional keys
    from env ALPACA_KEY_ID / ALPACA_SECRET_KEY. yfinance (bare US ticker) is the
    zero-config fallback when Alpaca isn't configured."""

    source = "alpaca"
    BASE = "https://data.alpaca.markets/v2"

    def _headers(self) -> dict:
        import os

        kid = os.environ.get("ALPACA_KEY_ID")
        sec = os.environ.get("ALPACA_SECRET_KEY")
        return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec} if kid and sec else {}

    def last_price(self, symbol: str) -> Decimal | None:
        try:
            import requests

            r = requests.get(f"{self.BASE}/stocks/{symbol}/trades/latest",
                             headers=self._headers(), params={"feed": "iex"}, timeout=5)
            r.raise_for_status()
            p = (r.json().get("trade") or {}).get("p")
            return _dec(p) if p is not None else None
        except Exception:
            return None

    def historical(
        self, symbol: str, bar_minutes: int, start: datetime, end: datetime
    ) -> list[dict]:
        try:
            import requests

            params = {
                "timeframe": _alpaca_timeframe(bar_minutes),
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
                "limit": 10000, "feed": "iex",
            }
            url = f"{self.BASE}/stocks/{symbol}/bars"
            out: list[dict] = []
            while True:
                r = requests.get(url, headers=self._headers(), params=params, timeout=10)
                r.raise_for_status()
                j = r.json()
                for b in j.get("bars") or []:
                    out.append(_candle(_parse_iso(b["t"]), b["o"], b["h"], b["l"], b["c"],
                                       b.get("v") or b.get("n")))
                token = j.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
            return out
        except Exception:
            return []


def _openbb_interval(bar_minutes: int) -> str:
    if bar_minutes >= 1440:
        return "1d"
    if bar_minutes % 60 == 0 and bar_minutes >= 60:
        return f"{bar_minutes // 60}h"
    return f"{bar_minutes}m"


def _obb_to_candles(df) -> list[dict]:
    """OpenBB `.to_dataframe()` → candle dicts. OpenBB uses lowercase columns and
    a date index (or a 'date' column); handle both, fail-soft."""
    if df is None or len(df) == 0:
        return []
    if "date" in list(getattr(df, "columns", [])):
        df = df.set_index("date")
    out: list[dict] = []
    for i in range(len(df)):
        row, ts = df.iloc[i], df.index[i]
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=UTC)
        get = lambda name: (row[name] if name in row else None)  # noqa: E731
        out.append(_candle(ts, get("open"), get("high"), get("low"),
                           get("close"), get("volume")))
    return out


class OpenBBData:
    """Optional richer data via the OpenBB Platform (~100 providers behind one
    API; free no-key providers: Yahoo / CBOE / yfinance; NSE via the .NS suffix).
    Used ONLY when a market is routed to it via config.MARKET_DATA_PROVIDER. Lazy-
    imported + fail-soft so the core never depends on `openbb` (AGPLv3 → kept an
    optional extra). Validate live once `pip install openbb` is present; inert by
    default (default config routes IN/US→yfinance, CRYPTO→ccxt)."""

    source = "openbb"

    def __init__(self, asset: str = "equity", suffix: str = "", provider: str | None = None):
        self.asset = asset
        self.suffix = suffix
        self.provider = provider

    def _ns(self):
        from openbb import obb
        return obb.crypto if self.asset == "crypto" else obb.equity

    def historical(
        self, symbol: str, bar_minutes: int, start: datetime, end: datetime
    ) -> list[dict]:
        try:
            df = self._ns().price.historical(
                f"{symbol}{self.suffix}", interval=_openbb_interval(bar_minutes),
                start_date=start.date().isoformat(), end_date=end.date().isoformat(),
                provider=self.provider,
            ).to_dataframe()
            return _obb_to_candles(df)
        except Exception:
            return []

    def last_price(self, symbol: str) -> Decimal | None:
        from datetime import timedelta
        try:
            end = datetime.now(UTC)
            bars = self.historical(symbol, 1440, end - timedelta(days=5), end)
            return bars[-1]["close"] if bars else None
        except Exception:
            return None

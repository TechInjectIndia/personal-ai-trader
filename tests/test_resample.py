"""
F6 — resample_candles integration tests.

resample_candles is SQL-bound (Postgres date_bin), so we test it against the
local `helm` DB using a throwaway symbol ('TEST_F6_*'). Each test inserts a
hand-built 1-min series for TODAY (IST), runs the resample, asserts OHLC +
session-anchored buckets, then cleans up its rows.

Skips entirely if the local DB is unreachable (mirrors other DB-touching tests).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from helm.data.store import conn, resample_candles, todays_candles

IST = ZoneInfo("Asia/Kolkata")


def _db_up() -> bool:
    try:
        with conn() as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="local helm DB unavailable")


def _today_at(hour: int, minute: int) -> datetime:
    now = datetime.now(IST)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _insert_bars(symbol: str, bars: list[dict]) -> None:
    with conn() as c:
        for b in bars:
            c.execute(
                """
                INSERT INTO candles_1m (market, symbol, bar_ts, open, high, low, close, tick_count)
                VALUES ('IN', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (market, symbol, bar_ts) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                    close = EXCLUDED.close, tick_count = EXCLUDED.tick_count
                """,
                (symbol, b["bar_ts"], b["open"], b["high"], b["low"],
                 b["close"], b["tick_count"]),
            )


def _cleanup(symbol: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM candles_1m WHERE symbol = %s", (symbol,))


def _bar(ts: datetime, o: str, h: str, lo: str, cl: str, ticks: int) -> dict:
    return {
        "bar_ts": ts,
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(lo),
        "close": Decimal(cl),
        "tick_count": ticks,
    }


def test_5min_ohlc_aggregation_and_session_anchor() -> None:
    """09:15-09:30 → three 5-min buckets anchored at 09:15/09:20/09:25, each with
    first-open, max-high, min-low, last-close, summed ticks."""
    symbol = "TEST_F6_OHLC"
    base = _today_at(9, 15)
    # 15 one-min bars. Bucket 1 = 09:15-09:19, bucket 2 = 09:20-09:24, etc.
    bars = []
    for i in range(15):
        o = Decimal("100") + Decimal(i)
        bars.append(_bar(
            base + timedelta(minutes=i),
            str(o),                       # open
            str(o + Decimal("2")),        # high
            str(o - Decimal("1")),        # low
            str(o + Decimal("0.5")),      # close
            ticks=i + 1,
        ))
    try:
        _cleanup(symbol)
        _insert_bars(symbol, bars)
        rows = resample_candles(symbol, 5)
        assert len(rows) == 3

        # Bucket 1: bars 0-4 (open=100, last close = bar4 close = 104.5)
        b1 = rows[0]
        assert b1["bar_ts"].astimezone(IST).strftime("%H:%M") == "09:15"
        assert Decimal(b1["open"]) == Decimal("100")          # first 1-min open
        assert Decimal(b1["high"]) == Decimal("106")          # bar4 high = 104+2
        assert Decimal(b1["low"]) == Decimal("99")            # bar0 low = 100-1
        assert Decimal(b1["close"]) == Decimal("104.5")       # bar4 close
        assert int(b1["tick_count"]) == sum(range(1, 6))      # 1+2+3+4+5

        # Bucket 2 anchored at 09:20, bucket 3 at 09:25.
        assert rows[1]["bar_ts"].astimezone(IST).strftime("%H:%M") == "09:20"
        assert rows[2]["bar_ts"].astimezone(IST).strftime("%H:%M") == "09:25"
        assert Decimal(rows[1]["open"]) == Decimal("105")     # bar5 open
        assert int(rows[2]["tick_count"]) == sum(range(11, 16))  # bars 10-14
    finally:
        _cleanup(symbol)


def test_15min_buckets_anchored_to_session_open() -> None:
    symbol = "TEST_F6_15M"
    base = _today_at(9, 15)
    bars = [
        _bar(base + timedelta(minutes=i), "100", "100", "100", "100", 1)
        for i in range(30)
    ]
    try:
        _cleanup(symbol)
        _insert_bars(symbol, bars)
        rows = resample_candles(symbol, 15)
        assert len(rows) == 2
        assert rows[0]["bar_ts"].astimezone(IST).strftime("%H:%M") == "09:15"
        assert rows[1]["bar_ts"].astimezone(IST).strftime("%H:%M") == "09:30"
    finally:
        _cleanup(symbol)


def test_partial_final_bucket() -> None:
    """Three 1-min bars (09:15-09:17) → exactly one (partial) 5-min bucket whose
    OHLC spans those three bars."""
    symbol = "TEST_F6_PARTIAL"
    base = _today_at(9, 15)
    bars = [
        _bar(base + timedelta(minutes=0), "100", "101", "99", "100.5", 3),
        _bar(base + timedelta(minutes=1), "100.5", "102", "100", "101.5", 4),
        _bar(base + timedelta(minutes=2), "101.5", "101.8", "100.2", "100.8", 5),
    ]
    try:
        _cleanup(symbol)
        _insert_bars(symbol, bars)
        rows = resample_candles(symbol, 5)
        assert len(rows) == 1
        b = rows[0]
        assert Decimal(b["open"]) == Decimal("100")      # first open
        assert Decimal(b["high"]) == Decimal("102")      # max high
        assert Decimal(b["low"]) == Decimal("99")        # min low
        assert Decimal(b["close"]) == Decimal("100.8")   # last close
        assert int(b["tick_count"]) == 12                # 3+4+5
    finally:
        _cleanup(symbol)


def test_minutes_one_is_todays_candles_identity() -> None:
    symbol = "TEST_F6_IDENTITY"
    base = _today_at(9, 15)
    bars = [
        _bar(base + timedelta(minutes=i), "100", "101", "99", "100.5", i + 1)
        for i in range(4)
    ]
    try:
        _cleanup(symbol)
        _insert_bars(symbol, bars)
        # minutes<=1 short-circuits to todays_candles → identical rows.
        rs = resample_candles(symbol, 1)
        tc = todays_candles(symbol)
        assert rs == tc
        assert len(rs) == 4
    finally:
        _cleanup(symbol)

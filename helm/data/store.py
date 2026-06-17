"""Postgres connection + small query helpers."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from helm.config import PG_DSN

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@contextmanager
def conn() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Yield a connection with autocommit + dict row factory.

    The `dict[str, Any]` row-type parameter tells the type checker that cursor
    rows are dicts (we pass `row_factory=dict_row`), so `row["col"]` access
    typechecks across the codebase instead of looking like tuple indexing.

    DSN defaults to `helm.config.PG_DSN` (local `dbname=helm`). `HELM_DSN` /
    `HELM_SEARCH_PATH` env vars override it — used only to point at an isolated
    test schema during development; unset in production, so behaviour is
    unchanged.
    """
    dsn = os.environ.get("HELM_DSN", PG_DSN)
    c = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    search_path = os.environ.get("HELM_SEARCH_PATH")
    if search_path:
        # SET search_path cannot use a bind param, so validate as a bare SQL
        # identifier before interpolation (defense against env-var SQL injection).
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", search_path):
            raise ValueError(f"invalid HELM_SEARCH_PATH: {search_path!r}")
        c.execute(f"SET search_path TO {search_path}")
    try:
        yield c
    finally:
        c.close()


def init_schema() -> None:
    """Run schema.sql idempotently."""
    sql = SCHEMA_PATH.read_text()
    with conn() as c:
        c.execute(sql)


def insert_tick(ts: datetime, symbol: str, ltp: Decimal, volume: int | None,
                raw: dict | None, market: str = "IN") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO ticks (ts, symbol, market, ltp, volume, raw) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
            (ts, symbol, market, ltp, volume, json.dumps(raw) if raw else None),
        )


def insert_audit(actor: str, event: str, detail: dict | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO audit (actor, event, detail) VALUES (%s, %s, %s::jsonb)",
            (actor, event, json.dumps(detail) if detail else None),
        )


def get_setting(key: str) -> Any:
    """Return the JSON value stored under `key`, or None if absent."""
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def all_settings() -> dict[str, Any]:
    """Snapshot of every row in the settings table."""
    with conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


def set_setting(key: str, value: Any, actor: str = "dashboard") -> None:
    """Upsert a setting; serialised as JSONB."""
    with conn() as c:
        c.execute(
            """
            INSERT INTO settings (key, value, updated_by)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_ts = now(),
                updated_by = EXCLUDED.updated_by
            """,
            (key, json.dumps(value), actor),
        )


def latest_ticks(symbol: str, limit: int = 100) -> list[dict[str, Any]]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT ts, ltp, volume FROM ticks WHERE symbol = %s ORDER BY ts DESC LIMIT %s",
                (symbol, limit),
            )
        )


def todays_candles(symbol: str, market: str = "IN",
                   tz: str = "Asia/Kolkata") -> list[dict[str, Any]]:
    """1-min candles for the current trading day (in market timezone `tz`),
    chronological. tz defaults to IST so the IN path is byte-identical."""
    with conn() as c:
        return list(
            c.execute(
                """
                SELECT bar_ts, open, high, low, close, tick_count
                FROM candles_1m
                WHERE symbol = %(symbol)s AND market = %(market)s
                  AND bar_ts >= date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s
                ORDER BY bar_ts ASC
                """,
                {"symbol": symbol, "market": market, "tz": tz},
            )
        )


def resample_candles(
    symbol: str, minutes: int, lookback_bars: int = 0, market: str = "IN",
    tz: str = "Asia/Kolkata", anchor_minutes: int = 555,
) -> list[dict[str, Any]]:
    """Aggregate today's `candles_1m` into `minutes`-minute OHLC bars.

    Buckets are anchored to the 09:15 IST session open via Postgres `date_bin`
    (PG14+), so 5-min buckets align to 09:15-09:20, 09:20-09:25, ... and 15-min
    buckets to 09:15/09:30/09:45. OHLC aggregation mirrors `roll_minute_candles`
    one level up: open = first 1-min open, high = max, low = min, close = last
    1-min close, tick_count = summed. Returns the SAME dict shape strategies
    expect — {bar_ts, open, high, low, close, tick_count} — in chronological
    order, so no strategy code needs to know it isn't 1-min data.

    `minutes <= 1` short-circuits to `todays_candles` (byte-identical 1-min path)
    so existing strategies are unaffected.

    `lookback_bars` is reserved for a future rolling-window limit; at today's
    scale all of a session's N-min bars are cheap, so it is currently unused.
    """
    if minutes <= 1:
        return todays_candles(symbol, market, tz)
    with conn() as c:
        return list(
            c.execute(
                """
                WITH anchor AS (
                    SELECT (date_trunc('day', now() AT TIME ZONE %(tz)s)
                            + make_interval(mins => %(anchor)s))
                           AT TIME ZONE %(tz)s AS open_ts
                )
                SELECT
                    date_bin(make_interval(mins => %(minutes)s), bar_ts,
                             (SELECT open_ts FROM anchor))  AS bar_ts,
                    (array_agg(open  ORDER BY bar_ts ASC))[1]  AS open,
                    MAX(high)                                  AS high,
                    MIN(low)                                   AS low,
                    (array_agg(close ORDER BY bar_ts DESC))[1] AS close,
                    SUM(tick_count)                            AS tick_count
                FROM candles_1m
                WHERE symbol = %(symbol)s AND market = %(market)s
                  AND bar_ts >= date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s
                GROUP BY 1
                ORDER BY bar_ts ASC
                """,
                {"minutes": minutes, "symbol": symbol, "market": market, "tz": tz,
                 "anchor": anchor_minutes},
            )
        )


def first_candle_open_at_or_after(symbol: str, at_ts: datetime,
                                  market: str = "IN") -> Decimal | None:
    """Open of the first 1-min candle with bar_ts >= at_ts (realistic fill bar).

    Returns None if no such bar exists yet (live-intraday: the next bar hasn't
    formed). `at_ts` is tz-aware; bar_ts is timestamptz so the comparison is
    timezone-correct regardless of IST/UTC representation. Indexed by
    candles_bar_ts, so the lookup is cheap.
    """
    with conn() as c:
        row = c.execute(
            """
            SELECT open FROM candles_1m
            WHERE symbol = %s AND market = %s AND bar_ts >= %s
            ORDER BY bar_ts ASC
            LIMIT 1
            """,
            (symbol, market, at_ts),
        ).fetchone()
    return Decimal(row["open"]) if row else None


def roll_minute_candles(market: str = "IN") -> int:
    """
    Fold this market's raw ticks into 1-minute OHLC candles.

    Idempotent: re-running won't duplicate (ON CONFLICT DO UPDATE on the
    per-market candle key). Safe to call from cron after every tick poll.

    Returns: number of candle rows upserted.
    """
    with conn() as c:
        rows = c.execute(
            """
            INSERT INTO candles_1m (market, symbol, bar_ts, open, high, low, close, tick_count)
            SELECT
                %s,
                symbol,
                date_trunc('minute', ts) AS bar_ts,
                (array_agg(ltp ORDER BY ts ASC))[1]      AS open,
                MAX(ltp)                                  AS high,
                MIN(ltp)                                  AS low,
                (array_agg(ltp ORDER BY ts DESC))[1]     AS close,
                COUNT(*)                                  AS tick_count
            FROM ticks
            WHERE ts >= now() - interval '15 minutes' AND market = %s
            GROUP BY symbol, date_trunc('minute', ts)
            ON CONFLICT (market, symbol, bar_ts) DO UPDATE SET
                high       = EXCLUDED.high,
                low        = EXCLUDED.low,
                close      = EXCLUDED.close,
                tick_count = EXCLUDED.tick_count
            RETURNING 1
            """,
            (market, market),
        ).fetchall()
        return len(rows)

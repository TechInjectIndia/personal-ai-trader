"""Postgres connection + small query helpers."""

from __future__ import annotations

import json
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
    """
    c = psycopg.connect(PG_DSN, autocommit=True, row_factory=dict_row)
    try:
        yield c
    finally:
        c.close()


def init_schema() -> None:
    """Run schema.sql idempotently."""
    sql = SCHEMA_PATH.read_text()
    with conn() as c:
        c.execute(sql)


def insert_tick(ts: datetime, symbol: str, ltp: Decimal, volume: int | None, raw: dict | None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO ticks (ts, symbol, ltp, volume, raw) VALUES (%s, %s, %s, %s, %s::jsonb)",
            (ts, symbol, ltp, volume, json.dumps(raw) if raw else None),
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


def todays_candles(symbol: str) -> list[dict[str, Any]]:
    """1-min candles for today, in chronological order."""
    with conn() as c:
        return list(
            c.execute(
                """
                SELECT bar_ts, open, high, low, close, tick_count
                FROM candles_1m
                WHERE symbol = %s
                  AND bar_ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
                ORDER BY bar_ts ASC
                """,
                (symbol,),
            )
        )


def first_candle_open_at_or_after(symbol: str, at_ts: datetime) -> Decimal | None:
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
            WHERE symbol = %s AND bar_ts >= %s
            ORDER BY bar_ts ASC
            LIMIT 1
            """,
            (symbol, at_ts),
        ).fetchone()
    return Decimal(row["open"]) if row else None


def roll_minute_candles() -> int:
    """
    Fold raw ticks into 1-minute OHLC candles.

    Idempotent: re-running won't duplicate (ON CONFLICT DO UPDATE). Safe to
    call from cron after every tick poll.

    Returns: number of candle rows upserted.
    """
    with conn() as c:
        rows = c.execute(
            """
            INSERT INTO candles_1m (symbol, bar_ts, open, high, low, close, tick_count)
            SELECT
                symbol,
                date_trunc('minute', ts) AS bar_ts,
                (array_agg(ltp ORDER BY ts ASC))[1]      AS open,
                MAX(ltp)                                  AS high,
                MIN(ltp)                                  AS low,
                (array_agg(ltp ORDER BY ts DESC))[1]     AS close,
                COUNT(*)                                  AS tick_count
            FROM ticks
            WHERE ts >= now() - interval '15 minutes'
            GROUP BY symbol, date_trunc('minute', ts)
            ON CONFLICT (symbol, bar_ts) DO UPDATE SET
                high       = EXCLUDED.high,
                low        = EXCLUDED.low,
                close      = EXCLUDED.close,
                tick_count = EXCLUDED.tick_count
            RETURNING 1
            """
        ).fetchall()
        return len(rows)

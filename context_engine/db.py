"""Service-side persistence for context_items / context_scores.

Reuses ``helm.data.store.conn`` (autocommit, dict_row, dbname=helm over the
local socket). This is the one-directional service -> helm import that the
architecture allows. The bot never calls anything here; it reaches context
only via HTTP.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from helm.data.store import conn

SCHEMA_PATH = Path(__file__).parent / "schema_context.sql"


def init_context_schema() -> None:
    """Run schema_context.sql idempotently (CREATE TABLE/INDEX IF NOT EXISTS)."""
    sql = SCHEMA_PATH.read_text()
    with conn() as c:
        c.execute(sql)


def db_ok() -> bool:
    """Cheap liveness probe for /health. Returns False instead of raising."""
    try:
        with conn() as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


def upsert_item(
    symbol: str,
    source: str,
    url: str,
    headline: str,
    body: str,
    published_ts: datetime | None,
) -> int | None:
    """Insert a context_item, deduping on UNIQUE(symbol, url).

    Returns the row id if a NEW row was inserted, else None (already present).
    A NULL/empty url is rejected (won't dedupe) — callers should synthesize one.
    """
    if not url:
        return None
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO context_items (symbol, source, url, headline, body, published_ts)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, url) DO NOTHING
            RETURNING id
            """,
            (symbol, source, url, headline, body, published_ts),
        ).fetchone()
    return int(row["id"]) if row else None


def insert_score(
    symbol: str,
    score: Decimal,
    half_life_min: int,
    rationale: str,
    item_ids: list[int],
) -> int:
    """Insert a context_scores row; returns its id."""
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO context_scores (symbol, score, half_life_min, rationale, item_ids)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (symbol, score, half_life_min, rationale, item_ids),
        ).fetchone()
    return int(row["id"])


def latest_score(symbol: str) -> dict[str, Any] | None:
    """Most-recent context_scores row for a symbol (via context_scores_recent),
    or None if the symbol has never been scored."""
    with conn() as c:
        return c.execute(
            """
            SELECT scored_ts, score, half_life_min, rationale
            FROM context_scores
            WHERE symbol = %s
            ORDER BY scored_ts DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()


def recent_items(symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recent context_items for a symbol (newest first)."""
    with conn() as c:
        return list(
            c.execute(
                """
                SELECT id, source, url, headline, body, published_ts, ingested_ts
                FROM context_items
                WHERE symbol = %s
                ORDER BY ingested_ts DESC
                LIMIT %s
                """,
                (symbol, limit),
            )
        )

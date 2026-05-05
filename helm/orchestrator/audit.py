"""
Append-only audit log (SQLite).

Every signal, decision, order, fill, rebalance, risk rejection, sleeve halt,
manual intervention, and vendor event is recorded here. Append-only — deletion
is forbidden by design.

PRD reference: FR-13 audit log, NFR-7 auditability.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,         -- ISO-8601 UTC
    actor       TEXT NOT NULL,         -- 'agent' | 'user' | 'system'
    event_type  TEXT NOT NULL,         -- 'signal' | 'decision' | 'order' | 'fill' | ...
    entity_ref  TEXT,                  -- e.g. order_id, signal_id
    payload_json TEXT NOT NULL,
    hash_prev   TEXT,                  -- optional hash chain
    hash_self   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_event(ts);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_event(event_type);
"""


DEFAULT_DB_PATH = Path("helm_state/audit.db")


def init_db(path: Path = DEFAULT_DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


def record(
    event_type: str,
    payload: dict,
    *,
    actor: str = "agent",
    entity_ref: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """TODO Phase 3: also implement hash chaining."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO audit_event (ts, actor, event_type, entity_ref, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                actor,
                event_type,
                entity_ref,
                json.dumps(payload, default=str),
            ),
        )
        return cur.lastrowid

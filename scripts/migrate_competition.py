"""
Competition-league migration / backfill.

Brings an existing single-portfolio deployment up to the multi-competitor
schema. It is fully idempotent — safe to run any number of times:

  1. Runs init_schema() so the competition tables/columns exist.
  2. Registers the incumbent bot as competitor 'house-claude'.
  3. Seeds its isolated wallet from helm.config.WalletConfig.initial_capital_inr
     (only on first insert — a live wallet's realised state is preserved).
  4. Stamps every existing signals / decisions / paper_trades row that has no
     competitor_id yet with 'house-claude' so the league treats the legacy
     book as that competitor's.

All money uses Decimal; state changes are recorded via insert_audit.

Usage:
  python scripts/migrate_competition.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.config import DECIDER_MODEL_DEFAULT, WalletConfig
from helm.data.store import conn, init_schema, insert_audit

HOUSE_ID = "house-claude"
HOUSE_NAME = "House (Claude)"
HOUSE_BACKEND = "claude"
HOUSE_PERSONA = (
    "Incumbent house bot: Claude-decided intraday MIS on top NSE large caps, "
    "single shared cash pool, conservative risk gate."
)
HOUSE_AUTONOMY = "incumbent"

# Tables that gain an optional competitor_id and need legacy rows stamped.
STAMP_TABLES = ("signals", "decisions", "paper_trades")


def _upsert_house_competitor(c) -> bool:
    """Insert the incumbent competitor row. Returns True if a row was created."""
    row = c.execute(
        """
        INSERT INTO competitors
            (id, name, backend, model, persona, autonomy_level, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'active')
        ON CONFLICT (id) DO NOTHING
        RETURNING id
        """,
        (HOUSE_ID, HOUSE_NAME, HOUSE_BACKEND, DECIDER_MODEL_DEFAULT,
         HOUSE_PERSONA, HOUSE_AUTONOMY),
    ).fetchone()
    return row is not None


def _seed_house_wallet(c, initial_capital: Decimal) -> bool:
    """Seed the incumbent wallet. Returns True if a wallet row was created.

    ON CONFLICT DO NOTHING preserves a live wallet's realised P&L on re-runs.
    """
    row = c.execute(
        """
        INSERT INTO competitor_wallets
            (competitor_id, initial_capital_inr, available_inr, realized_pnl_inr)
        VALUES (%s, %s, %s, 0)
        ON CONFLICT (competitor_id) DO NOTHING
        RETURNING competitor_id
        """,
        (HOUSE_ID, initial_capital, initial_capital),
    ).fetchone()
    return row is not None


def _stamp_legacy_rows(c, table: str) -> int:
    """Stamp untagged rows in `table` as the house competitor. Returns count."""
    rows = c.execute(
        f"""
        UPDATE {table}
        SET competitor_id = %s
        WHERE competitor_id IS NULL
        RETURNING 1
        """,
        (HOUSE_ID,),
    ).fetchall()
    return len(rows)


def migrate() -> dict[str, int]:
    """Run the full idempotent migration. Returns a summary of changes."""
    init_schema()

    initial_capital = WalletConfig().initial_capital_inr
    summary: dict[str, int] = {}

    with conn() as c:
        summary["competitor_inserted"] = int(_upsert_house_competitor(c))
        summary["wallet_inserted"] = int(_seed_house_wallet(c, initial_capital))
        for table in STAMP_TABLES:
            summary[f"{table}_stamped"] = _stamp_legacy_rows(c, table)

    insert_audit("migrate_competition", "backfill", summary)
    return summary


def main() -> None:
    summary = migrate()
    print("Competition migration complete:")
    print(f"  competitor 'house-claude' inserted : {summary['competitor_inserted']}")
    print(f"  house wallet inserted              : {summary['wallet_inserted']}")
    for table in STAMP_TABLES:
        print(f"  {table:<14} rows stamped        : {summary[f'{table}_stamped']}")
    total_stamped = sum(summary[f"{t}_stamped"] for t in STAMP_TABLES)
    if (summary["competitor_inserted"] == 0
            and summary["wallet_inserted"] == 0
            and total_stamped == 0):
        print("  (nothing to do — already migrated)")


if __name__ == "__main__":
    main()

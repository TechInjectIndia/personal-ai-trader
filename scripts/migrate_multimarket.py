"""
FRD M2 migration — namespace market data, fractional qty, per-market wallets.

ADDITIVE + idempotent: it simply (re)runs the idempotent `schema.sql` (whose
multi-market section does the ALTERs / PK swaps / wallets table) and then prints
a verification summary. Safe on a live deployment — every legacy row defaults to
`market='IN'`, so the India book is byte-identical after migrating.

Usage:
    python scripts/migrate_multimarket.py            # migrate dbname=helm
    HELM_SEARCH_PATH=mm_test python scripts/migrate_multimarket.py   # a test schema
"""

from __future__ import annotations

import sys

from helm.data.store import conn, init_schema


def verify() -> list[str]:
    """Return a list of human-readable problems (empty == all good)."""
    problems: list[str] = []
    with conn() as c:
        have = {
            r["table_name"]
            for r in c.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_name = 'market' "
                "AND table_name IN "
                "('ticks','candles_1m','signals','paper_trades','daily_state')"
            )
        }
        for t in ("ticks", "candles_1m", "signals", "paper_trades", "daily_state"):
            if t not in have:
                problems.append(f"{t}.market column missing")

        qty_type = c.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'paper_trades' AND column_name = 'qty'"
        ).fetchone()["data_type"]
        if qty_type != "numeric":
            problems.append(f"paper_trades.qty is {qty_type}, expected numeric")

        cpk = c.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
            "WHERE conrelid = (current_schema() || '.candles_1m')::regclass "
            "AND contype = 'p'"
        ).fetchone()
        if not cpk or "market" not in cpk["d"]:
            problems.append(f"candles_1m PK is not market-scoped: {cpk and cpk['d']}")

        wallets = c.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = 'wallets'"
        ).fetchone()
        if not wallets:
            problems.append("wallets table missing")

        # No legacy row should be left without a market (DEFAULT 'IN' guarantees
        # this, but assert it so a partial migration is loud).
        for t in ("paper_trades", "signals", "candles_1m"):
            n = c.execute(
                f"SELECT count(*) AS n FROM {t} WHERE market IS NULL"  # noqa: S608 (fixed names)
            ).fetchone()["n"]
            if n:
                problems.append(f"{t} has {n} rows with NULL market")
    return problems


def seed_wallets() -> None:
    """Seed per-market wallet rows for non-IN markets (currency, initial, goal)
    from config.MARKET_WALLET_SEED. ON CONFLICT DO NOTHING so a re-run never
    clobbers a live balance. IN is intentionally absent (it uses live_wallet_config)."""
    from helm.config import MARKET_WALLET_SEED

    with conn() as c:
        for market, (currency, initial, goal) in MARKET_WALLET_SEED.items():
            c.execute(
                "INSERT INTO wallets (market, currency, initial_capital, goal_capital) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (market) DO NOTHING",
                (market, currency, initial, goal),
            )


def main() -> int:
    init_schema()
    seed_wallets()
    problems = verify()
    if problems:
        print("M2 migration verification FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("M2 migration OK: market columns, NUMERIC qty, per-market candle PK, "
          "wallets table all present; no NULL-market rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

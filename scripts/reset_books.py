#!/usr/bin/env python3
"""Fresh-start reset — archive the trading ledger, then return every book to ₹50k.

"Clean trading slate": archives + clears paper_trades, decisions, signals,
metrics_snapshots so every book (house + the 5 competitors) returns to its
initial capital with empty P&L / equity curves / activity log / economics.
Equity is computed live (initial + Σ realised over CLOSED trades), so clearing
the trades does the reset; the competitor_wallets cache is reset to initial too.

KEEPS the agents' learning — trade_retrospectives + improvement_proposals rows
survive; only the retros' trade_id FK is detached (set NULL) so the trades can
be deleted. decisions + signals are RETAINED: trade_retrospectives.decision_id
is NOT NULL with a NO-ACTION FK, so clearing decisions would force deleting the
retros (cascading the lessons away). Retaining them is harmless — they are
date-filtered out of every live path (economics/F5/activity all JOIN
paper_trades or filter "today", which are now empty) and old signals are
consumed=TRUE, so they never re-trade.

REVERSIBLE: every cleared table is copied to a timestamped `<name>_reset_<ts>`
archive table first. To restore, INSERT back from the archive.

    python scripts/reset_books.py --dry-run   # show what WOULD happen
    python scripts/reset_books.py --yes       # actually do it
"""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from helm.data.store import conn, insert_audit

IST = ZoneInfo("Asia/Kolkata")
# Tables cleared for the fresh start. decisions/signals are NOT here — they are
# FK-pinned by trade_retrospectives.decision_id (NOT NULL), and they're inert
# (date-filtered out of all live logic), so they're retained as audit history.
CLEAR = ["paper_trades", "metrics_snapshots"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    ap.add_argument("--yes", action="store_true", help="confirm the destructive reset")
    args = ap.parse_args()

    ts = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    with conn() as c:
        counts = {t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in CLEAR}
        open_n = c.execute("SELECT count(*) AS n FROM paper_trades WHERE status='OPEN'").fetchone()["n"]
        retros = c.execute(
            "SELECT count(*) AS n FROM trade_retrospectives "
            "WHERE trade_id IS NOT NULL").fetchone()["n"]
        wallets = c.execute("SELECT count(*) AS n FROM competitor_wallets").fetchone()["n"]

        print(f"FRESH START (clean trading slate) — archive suffix _reset_{ts}")
        print(f"  clear : {counts}  (open positions now: {open_n})")
        print(f"  detach: {retros} retrospectives (rows KEPT; trade_id → NULL)")
        print("  keep  : decisions + signals (FK-pinned by retros; inert/date-filtered)")
        print(f"  reset : {wallets} competitor_wallets → available = initial; house recomputes to 50k")
        if open_n:
            print(f"  ⚠ {open_n} OPEN positions will be cleared without an exit fill — "
                  "run only when flat (market closed).")

        if args.dry_run or not args.yes:
            print("\n[no changes] pass --yes to execute (and ideally run while flat).")
            return 0

        # 1. Archive everything we touch (DDL; recoverable copies).
        for t in CLEAR + ["competitor_wallets", "trade_retrospectives"]:
            c.execute(f'CREATE TABLE "{t}_reset_{ts}" AS SELECT * FROM {t}')

        # 2. Reset atomically.
        with c.transaction():
            c.execute("UPDATE trade_retrospectives SET trade_id=NULL WHERE trade_id IS NOT NULL")
            c.execute("DELETE FROM paper_trades")       # child of decisions → safe to drop
            c.execute("DELETE FROM metrics_snapshots")  # equity-curve history → fresh
            c.execute("UPDATE competitor_wallets SET available_inr = initial_capital_inr")

        insert_audit("reset_books", "fresh_start", {
            "archive_suffix": ts, "cleared": counts, "open_cleared": open_n,
            "retros_detached": retros, "wallets_reset": wallets,
            "note": "clean trading slate → all 6 books to initial capital; learning kept",
        })
    print(f"\n✅ Reset complete. Every book back to its initial capital. "
          f"Archives: *_reset_{ts}. Learning (retros/proposals) preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

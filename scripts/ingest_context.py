"""Cron entry point for the Context Engine ingest+score pipeline.

Run via scripts/run_in_venv.sh every ~15 min in the IST market window on
weekdays. No-ops silently outside the window / on weekends (cron convention).
Writes an insert_audit per-run summary so the dashboard's audit panel shows
what happened.

This shares the SAME run_ingest() the service exposes via POST /refresh, so the
two paths can't drift. It is NOT on the cron yet — wired at P3a go-live.

Usage:
  python scripts/ingest_context.py                 # full WATCHLIST, window-gated
  python scripts/ingest_context.py --force         # ignore window (manual)
  python scripts/ingest_context.py --symbols INFY TCS
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from helm.data.store import insert_audit

from context_engine.db import init_context_schema
from context_engine.ingest import run_ingest

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="*", default=None,
                   help="Subset of WATCHLIST to ingest; default = all")
    p.add_argument("--force", action="store_true",
                   help="Bypass the IST window/weekday no-op (manual runs)")
    args = p.parse_args()

    load_dotenv(ENV_PATH, override=False)
    init_context_schema()

    syms = [s.upper() for s in args.symbols] if args.symbols else None
    result = run_ingest(syms, force=args.force)

    if result.skipped_window:
        return 0  # silent no-op outside window/weekend

    insert_audit(
        "ingest_context",
        "run_summary",
        {
            "symbols": result.symbols,
            "items_fetched": result.items_fetched,
            "items_new": result.items_new,
            "scored": result.scored,
            "errors": result.errors[:5],
        },
    )
    if args.force:
        print(f"[ingest_context] fetched={result.items_fetched} "
              f"new={result.items_new} scored={result.scored} "
              f"errors={len(result.errors)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

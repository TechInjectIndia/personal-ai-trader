"""
Forward-backtest a strategy on a market over a window (FRD M5). Decider-OFF.

    python scripts/run_backtest.py --strategy orb_15m --market IN --symbol RELIANCE --days 45
    python scripts/run_backtest.py --strategy vwap_reclaim_5m --market CRYPTO \
        --symbol BTC --days 180 --base-cap 1000 --persist

Prints the metrics JSON; `--persist` writes a `backtest_runs` row (the evidence
the M8 funding gate reads). Uses real time only for the default window — pass
explicit candles via the library API for fully reproducible runs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from helm.data.store import conn
from helm.eval.forward import backtest
from helm.markets import get_market
from helm.strategies import ACTIVE


def _strategy(name: str):
    for s in ACTIVE:
        if getattr(s, "name", None) == name:
            return s
    raise SystemExit(f"unknown strategy '{name}'; choices: {[s.name for s in ACTIVE]}")


def _code_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return None


def _persist(report, start: datetime, end: datetime) -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO backtest_runs (strategy, market, symbol, bar_minutes, start_ts,
                end_ts, decider, n_signals, n_trades, params, metrics, code_sha)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING id
            """,
            (report.strategy, report.market, report.symbol, report.bar_minutes, start, end,
             report.decider, report.n_signals, report.n_trades,
             json.dumps({"base_cap": float(report.base_cap)}),
             json.dumps(report.metrics), _code_sha()),
        ).fetchone()
    return row["id"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", required=True)
    p.add_argument("--market", default="IN")
    p.add_argument("--symbol", required=True)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--bar-minutes", type=int, default=None)
    p.add_argument("--base-cap", type=str, default=None)
    p.add_argument("--persist", action="store_true")
    args = p.parse_args()

    market = get_market(args.market)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    report = backtest(
        _strategy(args.strategy), market, args.symbol, start, end,
        bar_minutes=args.bar_minutes,
        base_cap=Decimal(args.base_cap) if args.base_cap else None,
    )
    print(json.dumps(report.as_row(), indent=2, default=str))
    if args.persist:
        rid = _persist(report, start, end)
        print(f"persisted backtest_runs id={rid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

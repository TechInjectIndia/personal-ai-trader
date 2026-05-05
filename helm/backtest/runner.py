"""
backtrader runner.

Loads cached OHLCV (DuckDB), runs a Strategy through `backtrader` with realistic
costs and slippage, emits an HTML report to `backtest_reports/`.

PRD reference: §11 backtest/runner.py, §11.4 backtest acceptance criteria.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass
class BacktestConfig:
    strategy_name: str
    start_date: datetime
    end_date: datetime
    starting_cash_inr: Decimal
    walkforward_train_years: int = 3
    walkforward_test_years: int = 1
    walkforward_step_months: int = 6


@dataclass
class BacktestReport:
    strategy_name: str
    sharpe: Decimal
    sortino: Decimal
    max_drawdown_pct: Decimal
    turnover: Decimal
    total_return_pct: Decimal
    trades: int
    report_path: Path


def run(config: BacktestConfig) -> BacktestReport:
    """TODO Phase 2 — wire backtrader.Cerebro, custom data feed, cost model."""
    raise NotImplementedError("backtest.runner.run — Phase 2")


def run_all() -> list[BacktestReport]:
    """Re-run all active strategies (called by monthly cron)."""
    raise NotImplementedError("backtest.runner.run_all — Phase 2")

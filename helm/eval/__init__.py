"""Deterministic replay/backtest harness for the self-improvement eval-gate
(FRD G3). Re-prices trades over recorded candles so a candidate change can be
judged on P&L impact, not just "compiles + smoke-passes"."""

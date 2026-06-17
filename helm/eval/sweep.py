"""
Cross-market backtest evidence sweep (S6).

Drives the forward backtester (helm.eval.forward) across every enabled market ×
the strategies allowed on it × its watchlist, persisting each result to
`backtest_runs` so the M8 funding gate + the leaderboard have backtest evidence
to read. Decider-OFF (no LLM cost); the only cost is data fetches.

`sweep_plan()` is the pure, testable planner (which combos will run, respecting
the session-strategy + context-strategy allowlist); `run_sweep()` executes them
fail-soft per combo.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from helm.config import live_risk_limits_for
from helm.eval.forward import backtest
from helm.markets import all_markets, get_market
from helm.strategies import ACTIVE


def _allowed(strat, market) -> bool:
    """Same rule as the live scanner: context strategies are IN-only; session
    strategies (ORB/gap-fade) can't run on a 24/7 venue (no session open)."""
    if getattr(strat, "requires_context", False) and market.key != "IN":
        return False
    if market.calendar.square_off_at() is None and getattr(strat, "session_required", False):
        return False
    return True


def _combos(market_keys: list[str]):
    for k in market_keys:
        m = get_market(k)
        for strat in ACTIVE:
            if not _allowed(strat, m):
                continue
            for sym in m.watchlist:
                yield k, m, strat, sym


def sweep_plan(market_keys: list[str] | None = None) -> list[tuple[str, str, str]]:
    """The (market, strategy, symbol) combos a sweep would run — pure (registry +
    ACTIVE only), so the plan is inspectable/testable without data or a DB."""
    keys = market_keys if market_keys is not None else list(all_markets().keys())
    return [(k, getattr(s, "name", type(s).__name__), sym) for k, _m, s, sym in _combos(keys)]


def _persist(report, start: datetime, end: datetime) -> None:
    from helm.data.store import conn

    with conn() as c:
        c.execute(
            "INSERT INTO backtest_runs (strategy,market,symbol,bar_minutes,start_ts,end_ts,"
            "decider,n_signals,n_trades,params,metrics,code_sha) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,NULL)",
            (report.strategy, report.market, report.symbol, report.bar_minutes, start, end,
             report.decider, report.n_signals, report.n_trades,
             json.dumps({"base_cap": float(report.base_cap), "sweep": True}),
             json.dumps(report.metrics)),
        )


def run_sweep(market_keys: list[str] | None = None, days: int = 180,
              persist: bool = False, now: datetime | None = None) -> list[dict]:
    """Run a backtest per combo (fail-soft). Returns one summary dict per combo
    that produced trades. `now` is injectable for determinism in tests."""
    keys = market_keys if market_keys is not None else list(all_markets().keys())
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out: list[dict] = []
    for k, m, strat, sym in _combos(keys):
        try:
            rep = backtest(strat, m, sym, start, end,
                           base_cap=live_risk_limits_for(k).max_position_inr)
        except Exception:
            continue  # one bad combo (data outage etc.) must not abort the sweep
        if persist:
            _persist(rep, start, end)
        out.append({"market": k, "strategy": rep.strategy, "symbol": sym,
                    "n_signals": rep.n_signals, "n_trades": rep.n_trades,
                    "net": rep.metrics.get("net", 0.0)})
    return out


__all__ = ["sweep_plan", "run_sweep"]

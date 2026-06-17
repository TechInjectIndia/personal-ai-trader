"""
Per-market go-live funding gate (FRD M8) — read-only.

Answers ONE question per market: is this making money, repeatably, before we
risk real capital? A market is READY only when BOTH evidence streams pass:
  * forward PAPER  — trailing-N closed paper_trades show net expectancy > 0,
    acceptable cost drag, and enough distinct trading days; and
  * BACKTEST       — the latest backtest_runs (M5) for the market net > 0.

This script NEVER places an order or arms live trading — funding is a manual
human action (see the runbook in docs/frd/M8). It writes a `go_live_readiness`
verdict row and, when a market first turns READY, pushes an Action Center item.

    python scripts/go_live_readiness.py            # evaluate all registered markets
    python scripts/go_live_readiness.py --persist  # + write verdict rows / alert
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal

from helm.config import HOUSE_TRADE_FILTER
from helm.data.store import conn
from helm.markets import all_markets


@dataclass(frozen=True)
class Gate:
    min_trades: int           # trailing closed paper trades required
    min_net_exp: Decimal      # net expectancy/trade must exceed this (₹/$ )
    max_cost_drag: Decimal    # charges / |gross| ceiling
    min_days: int             # distinct trading days of evidence


# Per-market thresholds (code-is-config). Crypto is STRICTER on cost drag — its
# ~0.10% taker fee dwarfs the ~0.05% NSE drag, so a thin edge dies there first.
READINESS: dict[str, Gate] = {
    "IN": Gate(30, Decimal("0"), Decimal("0.35"), 10),
    "US": Gate(30, Decimal("0"), Decimal("0.20"), 10),
    "CRYPTO": Gate(30, Decimal("0"), Decimal("0.50"), 14),
}
_DEFAULT_GATE = Gate(30, Decimal("0"), Decimal("0.40"), 10)


def _paper_metrics(market: str, n: int) -> dict:
    """Trailing-N closed house paper trades for the market."""
    with conn() as c:
        rows = list(c.execute(
            f"SELECT pnl_inr, net_pnl_inr, charges_inr, exit_ts FROM paper_trades "
            f"WHERE status = 'CLOSED' AND market = %s AND net_pnl_inr IS NOT NULL "
            f"AND {HOUSE_TRADE_FILTER} ORDER BY exit_ts DESC LIMIT %s",
            (market, n),
        ))
    closed = len(rows)
    net = sum((Decimal(r["net_pnl_inr"]) for r in rows), Decimal("0"))
    gross = sum((Decimal(r["pnl_inr"]) for r in rows if r["pnl_inr"] is not None), Decimal("0"))
    charges = sum((Decimal(r["charges_inr"]) for r in rows if r["charges_inr"] is not None),
                  Decimal("0"))
    days = len({r["exit_ts"].date() for r in rows if r["exit_ts"] is not None})
    expectancy = (net / closed) if closed else Decimal("0")
    cost_drag = (charges / abs(gross)) if gross != 0 else Decimal("0")
    return {"closed": closed, "net": net, "gross": gross, "charges": charges,
            "days": days, "expectancy": expectancy, "cost_drag": cost_drag}


def _backtest_metrics(market: str) -> dict:
    """Latest backtest_run per (strategy, symbol) for the market, summed."""
    with conn() as c:
        rows = list(c.execute(
            "SELECT DISTINCT ON (strategy, symbol) metrics "
            "FROM backtest_runs WHERE market = %s "
            "ORDER BY strategy, symbol, created_ts DESC",
            (market,),
        ))
    net = sum((Decimal(str(r["metrics"].get("net", 0))) for r in rows), Decimal("0"))
    trades = sum(int(r["metrics"].get("closed", 0)) for r in rows)
    return {"runs": len(rows), "net": net, "trades": trades}


def evaluate_market(market: str) -> dict:
    gate = READINESS.get(market, _DEFAULT_GATE)
    paper = _paper_metrics(market, gate.min_trades)
    bt = _backtest_metrics(market)
    reasons: list[str] = []

    if paper["closed"] < gate.min_trades:
        reasons.append(f"only {paper['closed']}/{gate.min_trades} closed paper trades")
    if paper["days"] < gate.min_days:
        reasons.append(f"only {paper['days']}/{gate.min_days} trading days of evidence")
    if paper["expectancy"] <= gate.min_net_exp:
        reasons.append(f"net expectancy {paper['expectancy']:.2f} <= {gate.min_net_exp}")
    if paper["cost_drag"] > gate.max_cost_drag:
        reasons.append(f"cost drag {paper['cost_drag']:.2f} > {gate.max_cost_drag}")
    paper_pass = (paper["closed"] >= gate.min_trades and paper["days"] >= gate.min_days
                  and paper["expectancy"] > gate.min_net_exp
                  and paper["cost_drag"] <= gate.max_cost_drag)

    if bt["runs"] == 0:
        reasons.append("no backtest evidence")
    elif bt["net"] <= 0:
        reasons.append(f"backtest net {bt['net']:.2f} <= 0")
    backtest_pass = bt["runs"] > 0 and bt["net"] > 0

    ready = paper_pass and backtest_pass
    return {
        "market": market, "paper_pass": paper_pass, "backtest_pass": backtest_pass,
        "ready": ready, "reasons": reasons,
        "metrics": {
            "paper": {k: float(v) if isinstance(v, Decimal) else v for k, v in paper.items()},
            "backtest": {k: float(v) if isinstance(v, Decimal) else v for k, v in bt.items()},
            "gate": {"min_trades": gate.min_trades, "min_net_exp": float(gate.min_net_exp),
                     "max_cost_drag": float(gate.max_cost_drag), "min_days": gate.min_days},
        },
    }


def _persist(result: dict) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO go_live_readiness (market, paper_pass, backtest_pass, ready, "
            "metrics, reasons) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
            (result["market"], result["paper_pass"], result["backtest_pass"],
             result["ready"], json.dumps(result["metrics"]), json.dumps(result["reasons"])),
        )
    # Surface a READY market to the human (idempotent by key). Funding is manual.
    if result["ready"]:
        from helm.dashboard.attention import enqueue

        enqueue(
            key=f"go_live_ready_{result['market']}",
            title=f"{result['market']} market is GO-LIVE READY",
            detail="Forward paper AND backtest both show positive net economics. "
                   "Fund manually at minimum size — nothing is armed automatically.",
            level="action", where="Self-Improvement", actor="go_live_readiness",
        )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--persist", action="store_true",
                   help="Write verdict rows + push an Action Center item for READY markets")
    p.add_argument("--market", default=None, help="Evaluate one market (default: all)")
    args = p.parse_args()

    keys = [args.market] if args.market else list(all_markets().keys())
    out = []
    for k in keys:
        r = evaluate_market(k)
        out.append(r)
        if args.persist:
            _persist(r)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Seed the freestyle competition cohort (idempotent).

Registers the four freestyle league agents — gemini, qwen, codex, opencode —
alongside the already-seeded incumbent 'house-claude' (see
scripts/migrate_competition.py). Each freestyle agent gets:
  * a `competitors` row (autonomy_level='freestyle'), with a distinct persona, and
  * an isolated `competitor_wallets` row seeded from WalletConfig.initial_capital_inr
    (₹50,000) — only on first insert, so a live wallet's P&L is never reset.

Backend authentication is separate: a competitor can exist here but only trade
once its CLI backend is authed (see docs/competition-backends-status.md;
`claude` + `opencode` work out of the box, the others need a one-time login).

Re-runnable: ON CONFLICT DO NOTHING. Run after migrate_competition.py.

Usage:
  python scripts/seed_competitors.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.config import WalletConfig
from helm.data.store import conn, init_schema, insert_audit

# (id, name, backend, model, persona). The model string is metadata for
# non-claude backends (their CLI adapters pick their own configured model);
# it documents the intended engine and feeds the dashboard.
COHORT: list[tuple[str, str, str, str, str]] = [
    (
        "gemini-momentum", "Gemini (Momentum)", "gemini", "gemini-2.5-flash",
        "Momentum breakout chaser: buys decisive intraday breakouts with "
        "visible follow-through on the 1-min tape; tight stops just under the "
        "breakout level; lets winners run toward an extended target.",
    ),
    (
        "qwen-meanrev", "Qwen (Mean-Reversion)", "qwen", "qwen-coder",
        "Mean-reversion / VWAP-reclaim trader: fades over-extended dips and "
        "buys reclaims of intraday support; modest targets, quick to cut; "
        "prefers liquid large caps over thin movers.",
    ),
    (
        "codex-trend", "Codex (Trend)", "codex", "gpt-5-codex",
        "Disciplined trend-follower: takes fewer, higher-conviction longs "
        "aligned with the prevailing intraday trend; wide-ish stops, patient "
        "targets; sits out chop and holds cash when there is no clean trend.",
    ),
    (
        "opencode-range", "Opencode (Range/ETF)", "opencode", "opencode/big-pickle",
        "Opportunistic range trader leaning on index/commodity ETFs "
        "(NIFTYBEES, GOLDBEES, SILVERBEES): buys lower-band bounces in "
        "well-defined ranges; small, frequent, low-volatility scalps.",
    ),
]

AUTONOMY = "freestyle"


def seed() -> dict[str, int]:
    init_schema()
    initial = WalletConfig().initial_capital_inr
    summary = {"competitors_inserted": 0, "wallets_inserted": 0}

    with conn() as c:
        for cid, name, backend, model, persona in COHORT:
            comp = c.execute(
                """
                INSERT INTO competitors
                    (id, name, backend, model, persona, autonomy_level, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'active')
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (cid, name, backend, model, persona, AUTONOMY),
            ).fetchone()
            summary["competitors_inserted"] += int(comp is not None)

            wallet = c.execute(
                """
                INSERT INTO competitor_wallets
                    (competitor_id, initial_capital_inr, available_inr, realized_pnl_inr)
                VALUES (%s, %s, %s, 0)
                ON CONFLICT (competitor_id) DO NOTHING
                RETURNING competitor_id
                """,
                (cid, initial, initial),
            ).fetchone()
            summary["wallets_inserted"] += int(wallet is not None)

    insert_audit("seed_competitors", "seed", {**summary, "cohort": [c[0] for c in COHORT]})
    return summary


def main() -> None:
    s = seed()
    print("Freestyle cohort seed complete:")
    print(f"  competitors inserted : {s['competitors_inserted']}")
    print(f"  wallets inserted     : {s['wallets_inserted']}")
    if s["competitors_inserted"] == 0 and s["wallets_inserted"] == 0:
        print("  (nothing to do — already seeded)")


if __name__ == "__main__":
    main()

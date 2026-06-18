"""
Seed the Claude personality cohort — the same reliable LLM (Claude) running
three deliberately divergent trading personas, for a CLEAN A/B on decision style.

The vendor league (scripts/seed_competitors.py) conflated two variables: model
capability/reliability AND decision style. Most of that signal was noise (broken
backends, slow vendors). Holding the model fixed at Claude and varying only the
PERSONA isolates the one variable that matters — how the agent thinks: which
stocks it picks, which signals it trusts, its risk appetite, and what it learns
from its own retrospectives (the G4 instinct ledger).

Each persona gets:
  * a `competitors` row (backend='claude', autonomy_level='freestyle'), and
  * an isolated `competitor_wallets` row seeded from WalletConfig (₹50,000),
    only on first insert (a live wallet's P&L is never reset).

house-claude remains the strategy-driven control baseline; Gemini stays on its
P&L kill-criterion as a cross-vendor sanity check. Idempotent: ON CONFLICT DO
NOTHING. After seeding, run `python scripts/plan_mandates.py` so each persona
picks its own universe (else it trades the default WATCHLIST slice).

Usage:
  python scripts/seed_personas.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helm.config import WalletConfig
from helm.data.store import conn, init_schema, insert_audit

MODEL = "claude-sonnet-4-6"  # same engine for all three — only the persona differs
AUTONOMY = "freestyle"

# (id, name, persona) — three divergent decision styles on one LLM.
PERSONAS: list[tuple[str, str, str]] = [
    (
        "claude-momentum", "Claude (Momentum)",
        "Momentum breakout specialist. Picks the day's strongest liquid "
        "large-caps showing decisive intraday breakouts with volume "
        "follow-through on the 1-min tape. Buys breaks of intraday highs and "
        "VWAP reclaims; stops just under the breakout pivot; lets winners run to "
        "extended targets (aim >=1.5R), accepting a sub-50% win rate for payoff. "
        "Avoids range-bound chop and counter-trend fades. Favours high-beta "
        "movers over sleepy names.",
    ),
    (
        "claude-meanrev", "Claude (Mean-Reversion)",
        "Mean-reversion contrarian. Fades over-extension: avoids/sells parabolic "
        "spikes and buys oversold reclaims back toward VWAP or the day's mean "
        "(z-score / Bollinger logic). Tight targets at the mean, quick to cut "
        "losers, many small trades. Avoids strong one-way trends where 'the mean' "
        "keeps moving. Favours stable, liquid large-caps with clean intraday "
        "ranges over trending high-beta names.",
    ),
    (
        "claude-sniper", "Claude (Risk-First Sniper)",
        "Risk-first capital preserver. Trades only A+ setups, at most ~2 per day, "
        "and holds cash otherwise. Tight stops, small size, optimises for low "
        "drawdown and a high win-rate over trade count. Skips anything ambiguous "
        "— would rather miss a move than take a mediocre entry. Sticks to the "
        "most liquid, predictable large-caps.",
    ),
]


def seed() -> dict[str, int]:
    init_schema()
    initial = WalletConfig().initial_capital_inr
    summary = {"competitors_inserted": 0, "wallets_inserted": 0}
    with conn() as c:
        for cid, name, persona in PERSONAS:
            comp = c.execute(
                """
                INSERT INTO competitors
                    (id, name, backend, model, persona, autonomy_level, status)
                VALUES (%s, %s, 'claude', %s, %s, %s, 'active')
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (cid, name, MODEL, persona, AUTONOMY),
            ).fetchone()
            summary["competitors_inserted"] += int(comp is not None)
            wallet = c.execute(
                """
                INSERT INTO competitor_wallets
                    (competitor_id, initial_capital_inr, available_inr, realized_pnl_inr)
                VALUES (%s, %s, %s, 0)
                ON CONFLICT (competitor_id, market) DO NOTHING
                RETURNING competitor_id
                """,
                (cid, initial, initial),
            ).fetchone()
            summary["wallets_inserted"] += int(wallet is not None)
    insert_audit("seed_personas", "seed",
                 {**summary, "personas": [p[0] for p in PERSONAS]})
    return summary


def main() -> None:
    s = seed()
    print("Claude persona cohort seed complete:")
    print(f"  competitors inserted : {s['competitors_inserted']}")
    print(f"  wallets inserted     : {s['wallets_inserted']}")
    if s["competitors_inserted"] == 0 and s["wallets_inserted"] == 0:
        print("  (nothing to do — already seeded)")
    else:
        print("  Next: python scripts/plan_mandates.py  (each persona picks its universe)")


if __name__ == "__main__":
    main()

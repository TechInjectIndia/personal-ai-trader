"""
Fund-only-winners leaderboard (multi-market UI). Read-only.

Ranks every agent × market × strategy combination by net expectancy and flags
the fund candidates (enough trades AND positive expectancy). This is the
"which combination has earned real capital?" view from the North Star.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from helm.dashboard.format import wrapped_table
from helm.dashboard.theme import apply_theme, page_header
from helm.eval.leaderboard import combo_scores, fund_candidates

st.set_page_config(page_title="Helm — Leaderboard", page_icon="🏆", layout="wide")
apply_theme()
page_header("Fund-only-winners Leaderboard",
            "Every agent × market × strategy, ranked by net expectancy", icon="🏆")

_CCY = {"INR": "₹", "USD": "$"}
c1, c2 = st.columns(2)
days = c1.slider("Look-back (days)", 7, 365, 30)
fund_min = c2.slider("Fund threshold — minimum trades", 10, 100, 30)

try:
    rows = combo_scores(days=days, min_trades=1)
    cand = {c.label() for c in fund_candidates(days=days, min_trades=fund_min)}
    if not rows:
        st.caption("No closed trades in the window yet.")
    else:
        df = pd.DataFrame([{
            "fund?": "✅" if r.label() in cand else "",
            "market": r.market, "agent": r.competitor_id or "house",
            "strategy": r.strategy or "—", "n": r.n,
            "net": f"{_CCY.get(r.currency, '')}{float(r.net):,.2f}",
            "exp/trade": f"{_CCY.get(r.currency, '')}{float(r.expectancy):,.2f}",
            "win%": round(float(r.win_pct), 1),
            "cost_drag": round(float(r.cost_drag), 2),
        } for r in rows])
        wrapped_table(df, right_align={"n", "win%", "cost_drag"})
        st.caption(
            f"{len(cand)} fund candidate(s): ≥{fund_min} trades AND positive net "
            "expectancy. The filter is sign-based, so currencies are each judged "
            "against zero — not compared against one another. Funding is manual.")
except Exception as exc:  # noqa: BLE001 — read-only page; degrade, don't crash
    st.info("No multi-market data yet — run `scripts/migrate_multimarket.py` and "
            f"let trades accumulate.  ({type(exc).__name__})")

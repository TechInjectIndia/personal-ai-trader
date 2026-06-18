"""
Backtest Lab — compose and run a backtest from the UI (no terminal).

Pick a market, a mindset (aggressive/balanced/defensive → sizing cap), the
strategies (signals) and entities (symbols), and a time window, then Run. Each
(strategy × symbol) is backtested deterministically (decider-OFF) and the result
is persisted to `backtest_runs` (so the Backtests + Go-Live pages pick it up).

Backtests are strategy-level by design (no LLM in the loop → reproducible/$0), so
"mindset" maps to the per-trade sizing cap rather than a decider persona.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import streamlit as st

from helm.config import live_risk_limits_for
from helm.data.store import conn
from helm.dashboard.format import wrapped_table
from helm.dashboard.theme import apply_theme, page_header
from helm.eval.forward import backtest
from helm.eval.sweep import _allowed
from helm.markets import all_markets
from helm.strategies import ACTIVE

st.set_page_config(page_title="Helm — Backtest Lab", page_icon="🧪", layout="wide")
apply_theme()
page_header("Backtest Lab", "Compose a backtest — mindset · market · entities · signals · period",
            icon="🧪")

_MINDSET = {"Balanced": Decimal("1.0"), "Aggressive": Decimal("1.5"), "Defensive": Decimal("0.5")}
_markets = all_markets()

c1, c2, c3 = st.columns([1.2, 1, 1])
mkey = c1.selectbox("Market", list(_markets),
                    format_func=lambda k: f"{_markets[k].name} · {_markets[k].currency}")
mindset = c2.selectbox("Mindset", list(_MINDSET),
                       help="Maps to the per-trade sizing cap (aggressive sizes bigger).")
days = c3.slider("Look-back (days)", 1, 60, 14,
                 help="1-min strategies are slow on long windows; keep short for those.")

market = _markets[mkey]
allowed = [s for s in ACTIVE if _allowed(s, market)]
_by_name = {s.name: s for s in allowed}
sym_opts = list(market.watchlist)

strat_names = st.multiselect("Strategies (signals)", list(_by_name),
                             default=[n for n in _by_name][:3])
symbols = st.multiselect("Entities (symbols)", sym_opts, default=sym_opts[:3])

base_cap = (live_risk_limits_for(mkey).max_position_inr * _MINDSET[mindset]).quantize(Decimal("1"))
n_combos = len(strat_names) * len(symbols)
st.caption(f"Sizing cap: **{market.currency} {base_cap}** ({mindset}) · "
           f"**{n_combos}** backtest(s) to run · decider-OFF, deterministic.")
if n_combos > 20:
    st.warning(f"{n_combos} combinations — this may take a while. Consider narrowing.")

if st.button("▶ Run backtest", type="primary", disabled=n_combos == 0):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows, persisted = [], 0
    prog = st.progress(0.0)
    done = 0
    for sn in strat_names:
        for sym in symbols:
            with st.spinner(f"Backtesting {sn} · {sym} …"):
                try:
                    rep = backtest(_by_name[sn], market, sym, start, end, base_cap=base_cap)
                    rows.append({"strategy": sn, "symbol": sym, "signals": rep.n_signals,
                                 "trades": rep.n_trades, "net": rep.metrics.get("net"),
                                 "win%": rep.metrics.get("win_pct"),
                                 "e2c": rep.metrics.get("e2c")})
                    with conn() as c:
                        c.execute(
                            "INSERT INTO backtest_runs (strategy,market,symbol,bar_minutes,"
                            "start_ts,end_ts,decider,n_signals,n_trades,params,metrics,code_sha) "
                            "VALUES (%s,%s,%s,%s,%s,%s,'take_all',%s,%s,%s::jsonb,%s::jsonb,NULL)",
                            (rep.strategy, mkey, sym, rep.bar_minutes, start, end,
                             rep.n_signals, rep.n_trades,
                             json.dumps({"base_cap": float(base_cap), "mindset": mindset,
                                         "via": "backtest_lab"}), json.dumps(rep.metrics)))
                    persisted += 1
                except Exception as exc:  # noqa: BLE001 — one bad combo shouldn't abort the run
                    rows.append({"strategy": sn, "symbol": sym, "signals": "—",
                                 "trades": "ERR", "net": str(exc)[:60], "win%": "", "e2c": ""})
            done += 1
            prog.progress(done / max(1, n_combos))
    prog.empty()
    st.success(f"Ran {n_combos} backtest(s); persisted {persisted} to backtest_runs.")
    if rows:
        wrapped_table(pd.DataFrame(rows).sort_values("net", ascending=False, na_position="last"),
                      right_align={"signals", "trades", "net", "win%", "e2c"})
else:
    st.info("Pick strategies + entities, then **Run backtest**. Results persist to the "
            "Backtests page and feed the Go-Live gate.")

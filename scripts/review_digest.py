#!/usr/bin/env python3
"""Session-start review digest — fetch how the "watch live" features performed.

Companion to REVIEW.md. Reads the persistent instrumentation (Postgres signals
+ logs/instrumentation/*.jsonl) for every feature on the watch-list and prints a
dated, glanceable digest so a new session can immediately assess live behaviour
and decide next actions — instead of re-deriving it from raw queries each time.

Read-only. Run at the start of every session in this repo (see REVIEW.md +
the session-start ritual in memory):

    python scripts/review_digest.py
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from helm.data.store import conn

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT_DIR = Path(__file__).resolve().parent.parent / "logs" / "instrumentation"
REAL = "pt.competitor_id IS NOT NULL AND left(pt.competitor_id, 3) <> 'zzz'"

# (heading, sql) — each read-only, guarded against empty results downstream.
BLOCKS: list[tuple[str, str]] = [
    ("Per-strategy economics (gross vs cost vs net, A/B incl. bbands_zscore_20)", f"""
        SELECT COALESCE(s.strategy,'unattributed') strategy, count(*) n,
               sum(pt.pnl_inr)::numeric(12,2) gross,
               sum(pt.charges_inr)::numeric(12,2) cost,
               sum(pt.net_pnl_inr)::numeric(12,2) net,
               round(avg(abs(pt.pnl_inr)) / NULLIF(avg(pt.charges_inr),0), 2) e2c,
               round(100.0*sum((pt.net_pnl_inr>0)::int)/count(*),1) win_pct
        FROM paper_trades pt
        LEFT JOIN decisions d ON d.id=pt.decision_id
        LEFT JOIN signals s ON s.id=d.signal_id
        WHERE pt.status='CLOSED' AND {REAL}
        GROUP BY 1 ORDER BY net"""),

    ("F2 min-edge gate — trades it blocked (SKIP below_min_edge_to_cost)", """
        SELECT count(*) FILTER (WHERE ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata') today,
               count(*) FILTER (WHERE ts >= now() - interval '7 days') last_7d,
               count(*) all_time
        FROM decisions
        WHERE verdict='SKIP' AND reasoning ILIKE '%below_min_edge_to_cost%'"""),

    ("#681 stop lock-in — ratchet events + house exit mix (14d)", """
        SELECT 'stop_tightened events 7d' k, count(*)::text v
        FROM audit WHERE event='stop_tightened' AND ts >= now()-interval '7 days'
        UNION ALL
        SELECT 'house exits 14d: '||COALESCE(exit_reason,'?'),
               count(*)::text||' (net Rs'||COALESCE(sum(net_pnl_inr),0)::text||')'
        FROM paper_trades
        WHERE status='CLOSED' AND exit_ts >= now()-interval '14 days'
          AND (competitor_id IS NULL OR competitor_id='house-claude')
        GROUP BY exit_reason ORDER BY 1"""),

    ("F4 bigger-move — avg target distance %% of recent signals (7d)", """
        SELECT strategy, count(*) n,
               round(avg(100.0*abs(target-entry_price)/entry_price),3) avg_target_pct
        FROM signals
        WHERE target IS NOT NULL AND ts >= now()-interval '7 days'
        GROUP BY 1 ORDER BY 1"""),

    ("F5 gate — does decider confidence predict outcome? (conf bucket vs net)", f"""
        WITH d AS (
          SELECT (regexp_match(de.reasoning,'conf=([0-9.]+)'))[1]::numeric conf,
                 pt.net_pnl_inr
          FROM decisions de JOIN paper_trades pt ON pt.decision_id=de.id
          WHERE de.verdict='TAKE' AND pt.status='CLOSED'
            AND de.reasoning ~ 'conf=' AND {REAL})
        SELECT CASE WHEN conf<0.5 THEN '1 <0.5' WHEN conf<0.7 THEN '2 0.5-0.7'
                    WHEN conf<0.85 THEN '3 0.7-0.85' ELSE '4 >=0.85' END bucket,
               count(*) n, round(100.0*sum((net_pnl_inr>0)::int)/count(*),1) win_pct,
               round(avg(net_pnl_inr),1) net_exp
        FROM d GROUP BY 1 ORDER BY 1"""),

    ("F7 Context Engine (SHADOW) — does the news score predict the next 30-min move?", """
        WITH s AS (
          SELECT cs.symbol, cs.scored_ts, cs.score::numeric sc,
            (SELECT c.close FROM candles_1m c
               WHERE c.symbol = cs.symbol AND c.bar_ts <= cs.scored_ts
               ORDER BY c.bar_ts DESC LIMIT 1) p0,
            (SELECT c.close FROM candles_1m c
               WHERE c.symbol = cs.symbol AND c.bar_ts >= cs.scored_ts + interval '30 minutes'
               ORDER BY c.bar_ts ASC LIMIT 1) p1
          FROM context_scores cs),
        p AS (SELECT sc, (p1 - p0) / NULLIF(p0, 0) ret
              FROM s WHERE p0 IS NOT NULL AND p1 IS NOT NULL)
        SELECT count(*) n_pairs,
               count(*) FILTER (WHERE abs(sc) >= 0.1) n_signal,
               round(100.0 * avg(((sc > 0) = (ret > 0))::int)
                     FILTER (WHERE abs(sc) >= 0.1), 1) dir_hit_pct,
               round(corr(sc::float8, ret::float8)::numeric, 3) pearson,
               round(avg(abs(ret)) * 100, 3) avg_abs_move_pct
        FROM p"""),

    ("F7 Context Engine — score coverage by symbol (rows accumulating in shadow)", """
        SELECT symbol, count(*) scores, round(avg(score), 3) avg_score, max(scored_ts) latest
        FROM context_scores GROUP BY 1 ORDER BY 1"""),

    ("Daily post-close loop cadence — runs per day (5d)", """
        SELECT date_trunc('day', started_ts AT TIME ZONE 'Asia/Kolkata')::date d,
               agent, outcome, count(*) n
        FROM agent_runs WHERE started_ts >= now()-interval '5 days'
          AND agent IN ('pm','engineer','tester')
        GROUP BY 1,2,3 ORDER BY 1 DESC, 2, 3"""),
]


def _render(rows: list[dict]) -> str:
    if not rows:
        return "  (no rows)\n"
    cols = list(rows[0].keys())
    out = ["  " + " | ".join(cols), "  " + "-+-".join("-" * len(c) for c in cols)]
    for r in rows:
        out.append("  " + " | ".join(str(r[c]) for c in cols))
    return "\n".join(out) + "\n"


def _instrumentation_summary() -> str:
    if not INSTRUMENT_DIR.exists():
        return "  (no logs/instrumentation/*.jsonl yet — features emit via Postgres only)\n"
    files = sorted(INSTRUMENT_DIR.glob("*.jsonl"))
    if not files:
        return "  (no .jsonl files yet)\n"
    lines = []
    for f in files:
        try:
            n = sum(1 for _ in f.open(encoding="utf-8"))
        except Exception:
            n = -1
        lines.append(f"  {f.stem}: {n} events  ({f})")
    return "\n".join(lines) + "\n"


def main() -> None:
    print(f"\n{'='*72}\n  REVIEW DIGEST — {datetime.now(IST):%Y-%m-%d %H:%M IST}")
    print(f"  (read REVIEW.md for hypotheses/checklists/cadence)\n{'='*72}")
    with conn() as c:
        for title, sql in BLOCKS:
            print(f"\n## {title.replace('%%','%')}")
            try:
                print(_render(c.execute(sql).fetchall()))
            except Exception as e:  # never let one bad block kill the digest
                print(f"  [query error: {e}]\n")
    print("\n## JSONL instrumentation (logs/instrumentation/)")
    print(_instrumentation_summary())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Render a human-readable Markdown report of the weekend self-improvement loop.

The loop (PM -> Engineer -> Tester) runs every Sunday (see `crontab -l`). This
script reads what it did straight from Postgres and writes a single glanceable
Markdown file per weekend to `logs/weekend-reports/<sunday-date>.md`, plus echoes
it to stdout.

Why this exists: so the weekend outcome can be read at a glance (or by an agent)
without re-deriving it from raw DB queries every time. Wired into the Sunday cron
after the Tester window so each weekend self-documents.

    python scripts/weekend_report.py                 # most recent Sunday
    python scripts/weekend_report.py --date 2026-06-07
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from helm.data.store import conn

IST = ZoneInfo("Asia/Kolkata")
REPORT_DIR = Path(__file__).resolve().parent.parent / "logs" / "weekend-reports"

# Trades from smoke/self-test fixtures must never pollute the P&L picture.
TEST_PREDICATE = "competitor_id IS NOT NULL AND competitor_id NOT LIKE 'zzz%'"


def most_recent_sunday(today: date) -> date:
    # Monday=0 .. Sunday=6; step back to the latest Sunday on/before today.
    return today - timedelta(days=(today.weekday() + 1) % 7)


def _window(sunday: date) -> tuple[datetime, datetime]:
    start = datetime.combine(sunday, datetime.min.time(), tzinfo=IST)
    return start, start + timedelta(days=1)


def _emoji(outcome: str | None) -> str:
    return {"ok": "🟢", "noop": "⚪", "error": "🔴",
            "verified": "🟢", "failed": "🔴"}.get((outcome or "").lower(), "🟡")


def build_report(sunday: date) -> str:
    start, end = _window(sunday)
    L: list[str] = []
    w = L.append

    w(f"# Weekend self-improvement loop — {sunday:%A %d %b %Y}")
    w("")
    w(f"_Generated {datetime.now(IST):%Y-%m-%d %H:%M IST} from Postgres. "
      "One file per weekend in `logs/weekend-reports/`._")
    w("")

    with conn() as c:
        # ── Loop health ──────────────────────────────────────────────
        cur = c.execute(
            """SELECT agent, outcome, count(*) n
               FROM agent_runs WHERE started_ts >= %s AND started_ts < %s
               GROUP BY agent, outcome ORDER BY agent, outcome""",
            (start, end),
        )
        rows = cur.fetchall()
        total = sum(r["n"] for r in rows)
        errors = sum(r["n"] for r in rows if r["outcome"] == "error")
        verdict = "🔴 errors present" if errors else (
            "🟢 clean" if total else "⚪ did not run")
        w(f"## TL;DR — {verdict}")
        w("")
        if not total:
            w("> **The loop produced no runs for this date.** Check the cron and "
              "the `autonomy_paused` setting.")
            w("")
        else:
            shipped = _count(c, "releases", start, end)
            verified = _count(c, "releases", start, end, "status='verified'")
            new_human = _count(c, "agent_tasks", start, end,
                               "status='needs_human'", col="created_ts")
            w(f"- **{total}** agent runs · **{errors}** errored")
            w(f"- **{shipped}** release(s) shipped · **{verified}** verified by Tester")
            w(f"- **{new_human}** item(s) escalated to a human this weekend")
            w("")

        # ── PM per-agent ─────────────────────────────────────────────
        cur = c.execute(
            """SELECT started_ts AT TIME ZONE 'Asia/Kolkata' ist, outcome, summary,
                      trace->>'competitor_id' competitor
               FROM agent_runs
               WHERE agent='pm' AND started_ts >= %s AND started_ts < %s
               ORDER BY started_ts""",
            (start, end),
        )
        pm = cur.fetchall()
        if pm:
            w("## PM weekly review (per agent)")
            w("")
            w("| Time | Agent | Result |")
            w("|---|---|---|")
            for r in pm:
                agent = r["competitor"] or "—"
                detail = r["summary"] or ("⚠️ errored (no summary)"
                                          if r["outcome"] == "error" else "")
                w(f"| {r['ist']:%H:%M} | {agent} | {_emoji(r['outcome'])} {detail} |")
            w("")

        # ── Engineer ─────────────────────────────────────────────────
        cur = c.execute(
            """SELECT started_ts AT TIME ZONE 'Asia/Kolkata' ist, outcome, summary
               FROM agent_runs
               WHERE agent='engineer' AND started_ts >= %s AND started_ts < %s
                 AND outcome <> 'noop'
               ORDER BY started_ts""",
            (start, end),
        )
        eng = cur.fetchall()
        noops = _count(c, "agent_runs", start, end,
                       "agent='engineer' AND outcome='noop'")
        w("## Engineer")
        w("")
        if eng:
            for r in eng:
                w(f"- {_emoji(r['outcome'])} `{r['ist']:%H:%M}` "
                  f"{r['summary'] or r['outcome']}")
        else:
            w("- No real task attempts.")
        w(f"- ⚪ {noops} idle slot(s) (`no tasks to claim`).")
        w("")

        # ── Tasks created ────────────────────────────────────────────
        cur = c.execute(
            """SELECT id, competitor_id, task_type, status, title
               FROM agent_tasks WHERE created_ts >= %s AND created_ts < %s
               ORDER BY id""",
            (start, end),
        )
        tasks = cur.fetchall()
        if tasks:
            w("## Tasks created this weekend")
            w("")
            w("| # | Agent | Type | Status | Title |")
            w("|---|---|---|---|---|")
            for t in tasks:
                title = (t["title"] or "")[:60]
                w(f"| {t['id']} | {t['competitor_id'] or '—'} | {t['task_type']} "
                  f"| {_emoji(t['status'])} {t['status']} | {title} |")
            w("")

        # ── Open needs_human (standing queue, not just this weekend) ──
        cur = c.execute(
            """SELECT id, competitor_id, title, spec->>'reason' reason
               FROM agent_tasks WHERE status='needs_human' ORDER BY id""")
        human = cur.fetchall()
        if human:
            w("## ⚠️ Open `needs_human` queue (waiting on you)")
            w("")
            for t in human:
                w(f"- **#{t['id']}** ({t['competitor_id'] or '—'}) — {t['title']}")
                if t["reason"]:
                    w(f"  - _{t['reason'][:200]}_")
            w("")

        # ── P&L snapshot (real money) ────────────────────────────────
        w("## P&L snapshot (real competitors, test rows excluded)")
        w("")
        cur = c.execute(
            f"""SELECT competitor_id, count(*) trades,
                       sum(net_pnl_inr)::numeric(12,2) net,
                       round(100.0*sum((net_pnl_inr>0)::int)/count(*),1) win_pct
                FROM paper_trades
                WHERE status='CLOSED' AND {TEST_PREDICATE}
                GROUP BY 1 ORDER BY net""")
        pnl = cur.fetchall()
        if pnl:
            w("| Agent | Closed | Net ₹ | Win % |")
            w("|---|---|---|---|")
            for r in pnl:
                flag = "🔴" if (r["net"] or 0) < 0 else "🟢"
                w(f"| {r['competitor_id']} | {r['trades']} | {flag} {r['net']} "
                  f"| {r['win_pct']}% |")
            tot = sum(r["net"] or 0 for r in pnl)
            tn = sum(r["trades"] for r in pnl)
            w(f"| **ALL** | **{tn}** | **{tot:.2f}** | — |")
            w("")
        cur = c.execute(
            f"""SELECT exit_reason, count(*) n, sum(net_pnl_inr)::numeric(12,2) net
                FROM paper_trades WHERE status='CLOSED' AND {TEST_PREDICATE}
                GROUP BY 1 ORDER BY net""")
        ex = cur.fetchall()
        if ex:
            w("**By exit reason:** " + " · ".join(
                f"{r['exit_reason']} {r['n']} (₹{r['net']})" for r in ex))
            w("")

    return "\n".join(L)


def _count(c, table, start, end, extra="", col="created_ts") -> int:
    if table == "agent_runs":
        col = "started_ts"
    where = f"{col} >= %s AND {col} < %s" + (f" AND {extra}" if extra else "")
    cur = c.execute(f"SELECT count(*) n FROM {table} WHERE {where}", (start, end))
    return cur.fetchone()["n"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="Sunday to report on (YYYY-MM-DD). "
                    "Default: most recent Sunday.")
    args = ap.parse_args()

    if args.date:
        sunday = date.fromisoformat(args.date)
    else:
        sunday = most_recent_sunday(datetime.now(IST).date())

    report = build_report(sunday)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{sunday:%Y-%m-%d}.md"
    out.write_text(report + "\n")
    print(report)
    print(f"\n[weekend_report] written to {out}")


if __name__ == "__main__":
    main()

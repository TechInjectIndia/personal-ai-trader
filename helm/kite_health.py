"""Kite access-token health — shared by the dashboard banner and the watchdog.

The token is refreshed every weekday at 06:10 IST by `scripts/kite_auto_login.py`
(cron `40 0 * * 1-5` UTC). It then expires the next 06:00 IST. So on a *weekday*
morning a healthy system has a refresh from today; on a *weekend* the last refresh
is Friday's and that is entirely normal — markets are closed and nothing trades.

A naive "last success > 25h ago" check therefore false-positives every weekend,
which trains the operator to ignore the banner and buries the one alert that
matters: a weekday refresh that silently failed. `token_health()` is calendar-
aware — it compares the last successful refresh against the most recent *expected*
refresh (the latest past weekday 06:10 IST whose grace window has elapsed) so it
only warns when a refresh that was actually due is missing or failed.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from helm.data.store import conn

IST = ZoneInfo("Asia/Kolkata")

# Cron fires the refresh at 06:10 IST; give it 15 min to finish before a missing
# refresh counts as "due and absent". 06:25 IST is still ~50 min before the
# 09:15 open, so a same-morning alert leaves time to fix it manually.
REFRESH_TIME = time(6, 10)
GRACE = timedelta(minutes=15)


def last_expected_refresh(now: datetime | None = None) -> datetime:
    """Most recent weekday 06:10 IST whose grace window (06:25) has already passed.

    This is the refresh that *should* exist by now. On a weekend or before a
    weekday's window has elapsed, it points back to the previous trading day's
    06:10 — so Friday's refresh remains "current" all weekend.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    day = now.date()
    for _ in range(8):  # at most a long weekend + a couple of holidays back
        cand = datetime.combine(day, REFRESH_TIME, tzinfo=IST)
        if cand.weekday() < 5 and now >= cand + GRACE:
            return cand
        day -= timedelta(days=1)
    # Fallback (shouldn't happen): the last computed candidate.
    return datetime.combine(day, REFRESH_TIME, tzinfo=IST)


def _latest_event():
    with conn() as c:
        return c.execute(
            "SELECT ts, event, detail FROM audit "
            "WHERE actor = 'kite_auto_login' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()


def token_health(now: datetime | None = None) -> dict:
    """Verdict on the daily token refresh.

    Returns a dict with `level` in {"ok", "stale", "failed", "never"} plus the
    backing audit row (`ts`, `event`, `detail`), `age_h`, and the `expected`
    refresh time used for the comparison. `level`:

      - never   : auto-login has never recorded an event
      - failed  : its most recent attempt logged a failure
      - stale   : last *success* predates the most recent due refresh
      - ok       : a successful refresh exists for the current trading day
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    row = _latest_event()
    if row is None:
        return {"level": "never", "ts": None, "event": None, "detail": None,
                "age_h": None, "expected": last_expected_refresh(now)}

    ts = row["ts"].astimezone(IST)
    expected = last_expected_refresh(now)
    base = {
        "ts": row["ts"],
        "event": row["event"],
        "detail": row["detail"],
        "age_h": (now - ts).total_seconds() / 3600,
        "expected": expected,
    }
    if row["event"] == "failed":
        return {"level": "failed", **base}
    # A success counts as current if it landed on/after the expected day's start.
    expected_morning = expected.replace(hour=0, minute=0, second=0, microsecond=0)
    if ts < expected_morning:
        return {"level": "stale", **base}
    return {"level": "ok", **base}

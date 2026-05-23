"""Weekday Kite access-token watchdog — proactive failure detection + self-heal.

The dashboard banner (helm.kite_health) flags a missing refresh, but only when a
human happens to load the page, and only from the audit log — it can't see a token
that was *recorded as refreshed* yet later killed by a concurrent Kite web/mobile
login (single-session enforcement). This script closes both gaps:

  1. ACTIVELY PROBES the live token with kite.profile() (works on the base Kite
     subscription — unlike ltp/quote/historical). That is the real proof.
  2. If the probe fails, or the audit log shows no refresh due for today, it
     SELF-HEALS by running scripts/kite_auto_login.py once, then re-probes.
  3. If it still can't get a working token, it ESCALATES loudly:
        - insert_audit('kite_watchdog', 'alert', {...})
        - appends a line to logs/alerts.log
        - exits non-zero (so a configured cron MAILTO fires)

No-ops silently on weekends. Cron'd weekday 06:25 IST (just after the 06:10
refresh) and 08:30 IST (last check before the 09:15 open). Manual run:

    source .venv/bin/activate
    python scripts/check_kite_token.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

from helm.data.store import insert_audit
from helm.kite_health import IST, token_health

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _REPO_ROOT / ".env"
_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"
_LOGIN_SCRIPT = _REPO_ROOT / "scripts" / "kite_auto_login.py"
_ALERTS_LOG = _REPO_ROOT / "logs" / "alerts.log"


def _probe() -> tuple[bool, str]:
    """Call kite.profile() with the current .env token. (ok, detail)."""
    load_dotenv(_ENV_PATH, override=True)
    api_key = os.environ.get("KITE_API_KEY")
    token = os.environ.get("KITE_ACCESS_TOKEN")
    if not api_key or not token:
        return False, "missing KITE_API_KEY / KITE_ACCESS_TOKEN in .env"
    try:
        k = KiteConnect(api_key=api_key)
        k.set_access_token(token)
        profile = k.profile()
        return True, f"profile ok for {profile.get('user_id')}"
    except Exception as exc:  # noqa: BLE001 — any failure means "token not usable"
        return False, f"{type(exc).__name__}: {exc}"


def _run_auto_login() -> tuple[bool, str]:
    """Trigger the auto-login script; (ok, tail-of-output)."""
    try:
        r = subprocess.run(
            [str(_VENV_PYTHON), str(_LOGIN_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_REPO_ROOT),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run kite_auto_login.py: {exc}"
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out[-500:]


def _escalate(reason: str, detail: dict) -> None:
    """Write the alert everywhere a human (or cron MAILTO) might see it."""
    ts = datetime.now(IST).strftime("%a %Y-%m-%d %H:%M:%S %Z")
    insert_audit("kite_watchdog", "alert", {"reason": reason, **detail})
    try:
        _ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ALERTS_LOG.open("a") as fh:
            fh.write(f"[{ts}] KITE TOKEN ALERT: {reason} | {detail}\n")
    except OSError as exc:
        print(f"warn: could not append to {_ALERTS_LOG} ({exc})", file=sys.stderr)


def main() -> None:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        print("weekend — watchdog no-op")
        return

    health = token_health(now)
    ok, detail = _probe()

    # Healthy: token works AND a refresh is on record for today's window.
    if ok and health["level"] == "ok":
        print(f"OK: {detail} (last refresh {health['level']})")
        return

    print(
        f"watchdog: probe_ok={ok} ({detail}); refresh_level={health['level']} "
        f"— attempting self-heal via kite_auto_login.py",
        file=sys.stderr,
    )
    healed, login_out = _run_auto_login()
    reprobe_ok, reprobe_detail = _probe()

    if reprobe_ok:
        insert_audit(
            "kite_watchdog",
            "healed",
            {"trigger": detail, "before_level": health["level"]},
        )
        print(f"healed: re-login succeeded, {reprobe_detail}")
        return

    _escalate(
        "token unusable and self-heal failed",
        {
            "probe": detail,
            "reprobe": reprobe_detail,
            "auto_login_ok": healed,
            "auto_login_out": login_out,
            "refresh_level": health["level"],
        },
    )
    sys.exit(
        f"KITE WATCHDOG ALERT: token still unusable after self-heal "
        f"(probe: {reprobe_detail}). See logs/alerts.log."
    )


if __name__ == "__main__":
    main()

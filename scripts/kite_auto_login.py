"""
Kite Connect daily auto-login.

Kite access tokens expire every day at 06:00 IST. This script does the full
login flow headlessly:

  1. POST username + password to /api/login            → request_id
  2. POST request_id + locally-generated TOTP code     → session cookies
  3. GET /connect/login?api_key=...&v=3 (no redirects) → request_token
  4. KiteConnect.generate_session(request_token, ...)  → access_token

The new access_token is written back to .env (replacing KITE_ACCESS_TOKEN=...)
so that all other scripts pick it up via python-dotenv. A best-effort
`pm2 restart helm-dashboard` reloads the dashboard's Kite client.

Cron'd at 40 0 * * 1-5 UTC (06:10 IST) so it runs ~10 min after token expiry,
well before the first market-data poll at 03:45 UTC / 09:15 IST. Manual run:

    source .venv/bin/activate
    python scripts/kite_auto_login.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pyotp
import requests
from dotenv import load_dotenv
from kiteconnect import KiteConnect

from helm.data.store import insert_audit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _REPO_ROOT / ".env"
load_dotenv(_ENV_PATH)

_LOGIN_URL = "https://kite.zerodha.com/api/login"
_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
_CONNECT_URL = "https://kite.zerodha.com/connect/login"
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) helm-auto-login/1.0"
_REDIRECT_HOPS = 10
_MAX_ATTEMPTS = 3       # transient network / TOTP-boundary blips shouldn't page anyone
_RETRY_BACKOFF_S = 5


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: {name} not set in environment / .env")
    return val


def _login(session: requests.Session, user_id: str, password: str) -> str:
    r = session.post(_LOGIN_URL, data={"user_id": user_id, "password": password})
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"/api/login failed: {body}")
    return body["data"]["request_id"]


def _twofa(session: requests.Session, user_id: str, request_id: str, totp_code: str) -> None:
    r = session.post(
        _TWOFA_URL,
        data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
            "skip_session": "",
        },
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"/api/twofa failed: {body}")


def _capture_request_token(session: requests.Session, api_key: str) -> str:
    """
    Hit /connect/login and walk the redirect chain manually until we see
    `request_token` in a Location header. We never actually visit the registered
    redirect URL (which may be a placeholder pointing nowhere reachable).
    """
    url = f"{_CONNECT_URL}?api_key={api_key}&v=3"
    for _ in range(_REDIRECT_HOPS):
        r = session.get(url, allow_redirects=False)
        loc = r.headers.get("Location")
        if not loc:
            raise RuntimeError(
                f"connect/login returned {r.status_code} with no redirect; body={r.text[:300]}"
            )
        qs = parse_qs(urlparse(loc).query)
        if "request_token" in qs:
            return qs["request_token"][0]
        url = urljoin(url, loc)
    raise RuntimeError("Exhausted redirect hops without finding request_token")


def _persist_access_token(access_token: str) -> None:
    if not _ENV_PATH.exists():
        sys.exit(f"ERROR: {_ENV_PATH} missing — cannot persist access token")
    text = _ENV_PATH.read_text()
    if re.search(r"^KITE_ACCESS_TOKEN=.*$", text, flags=re.M):
        text = re.sub(
            r"^KITE_ACCESS_TOKEN=.*$",
            f"KITE_ACCESS_TOKEN={access_token}",
            text,
            flags=re.M,
        )
    else:
        text += f"\nKITE_ACCESS_TOKEN={access_token}\n"
    _ENV_PATH.write_text(text)


def _restart_dashboard() -> None:
    try:
        subprocess.run(
            ["pm2", "restart", "helm-dashboard"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # Dashboard reload is best-effort; the bot scripts pick up .env at start.
        print(f"warn: pm2 restart helm-dashboard skipped ({exc})", file=sys.stderr)


def _one_attempt(api_key: str, api_secret: str, user_id: str, password: str,
                 totp_secret: str) -> dict:
    """Run the full headless login once and return the generate_session payload.

    A fresh session and a freshly-generated TOTP code per call, so a retry after
    a 30-second TOTP rollover or a dropped connection starts clean.
    """
    session = requests.Session()
    session.headers["User-Agent"] = _USER_AGENT
    request_id = _login(session, user_id, password)
    _twofa(session, user_id, request_id, pyotp.TOTP(totp_secret).now())
    request_token = _capture_request_token(session, api_key)
    kite = KiteConnect(api_key=api_key)
    return kite.generate_session(request_token, api_secret=api_secret)


def main() -> None:
    api_key = _require("KITE_API_KEY")
    api_secret = _require("KITE_API_SECRET")
    user_id = _require("KITE_USER_ID")
    password = _require("KITE_PASSWORD")
    totp_secret = _require("KITE_TOTP_SECRET")

    data = None
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            data = _one_attempt(api_key, api_secret, user_id, password, totp_secret)
            break
        except Exception as exc:  # noqa: BLE001 — any failure is retryable here
            last_exc = exc
            print(f"attempt {attempt}/{_MAX_ATTEMPTS} failed: {exc}", file=sys.stderr)
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_S)

    if data is None:
        insert_audit(
            "kite_auto_login",
            "failed",
            {"error": str(last_exc)[:500], "attempts": _MAX_ATTEMPTS},
        )
        sys.exit(f"kite_auto_login failed after {_MAX_ATTEMPTS} attempts: {last_exc}")

    access_token = data["access_token"]
    _persist_access_token(access_token)
    _restart_dashboard()

    insert_audit(
        "kite_auto_login",
        "refreshed",
        {"user_id": data.get("user_id"), "login_time": str(data.get("login_time"))},
    )
    print(f"OK: access_token refreshed for user_id={data.get('user_id')}")


if __name__ == "__main__":
    main()

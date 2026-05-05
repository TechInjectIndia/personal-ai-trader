"""
Kite Connect daily access-token helper.

Kite tokens expire every day at 06:00 IST. To get a fresh one:

  1. Open this URL in a browser (replace YOUR_API_KEY):
        https://kite.trade/connect/login?api_key=YOUR_API_KEY&v=3
     Sign in. Kite redirects to your registered redirect URL with
     ?request_token=xxxxx&action=login&status=success appended.

  2. Copy the request_token value.

  3. Run:
        export KITE_API_KEY="..."
        export KITE_API_SECRET="..."
        python scripts/kite_login.py <request_token>

  4. The script prints the access_token. Save it:
        export KITE_ACCESS_TOKEN="paste-printed-token"

(In production, the orchestrator's kite-token-refresh job automates this.)
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

# Load helm/.env from the repo root (one level up from scripts/).
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python kite_login.py <request_token>")
    request_token = sys.argv[1]

    api_key = os.environ.get("KITE_API_KEY")
    api_secret = os.environ.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        sys.exit("ERROR: set KITE_API_KEY and KITE_API_SECRET env vars first.")

    kite = KiteConnect(api_key=api_key)
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        sys.exit(f"generate_session failed: {exc}")

    access_token = data["access_token"]
    print("ACCESS_TOKEN:", access_token)
    print("USER_ID:     ", data.get("user_id"))
    print("LOGIN_TIME:  ", data.get("login_time"))

    # Write it back into helm/.env so subsequent scripts pick it up automatically.
    if _ENV_PATH.exists():
        text = _ENV_PATH.read_text()
        if re.search(r"^KITE_ACCESS_TOKEN=.*$", text, flags=re.M):
            text = re.sub(r"^KITE_ACCESS_TOKEN=.*$", f"KITE_ACCESS_TOKEN={access_token}", text, flags=re.M)
        else:
            text += f"\nKITE_ACCESS_TOKEN={access_token}\n"
        _ENV_PATH.write_text(text)
        print(f"\n✓ Updated KITE_ACCESS_TOKEN in {_ENV_PATH}")
    else:
        print()
        print("No .env at", _ENV_PATH, "— set in your shell instead:")
        print(f'  export KITE_ACCESS_TOKEN="{access_token}"')


if __name__ == "__main__":
    main()

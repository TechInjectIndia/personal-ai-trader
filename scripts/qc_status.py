"""
helm — qc_status.py
Phase 1, Gate G4 — proves we can drive QuantConnect from a plain Python
script (no QC web UI), which is the foundation of vendor_watchdog.py and
kill_switch.py in the orchestrator.

What this does:
  1. Lists all your QC projects.
  2. Lists all currently running live deployments.
  3. Optionally stops a deployment given its deploy ID (--stop <id>).

Setup:
    export QC_USER_ID="<your numeric user id>"
    export QC_API_TOKEN="<the API token from quantconnect.com/account>"

Then:
    python qc_status.py                     # list projects + deployments
    python qc_status.py --stop <deploy_id>  # stop one live deployment

Pass criterion (G4):
  Listing projects returns DryRun_Zerodha + DryRun_IBKR.
  Stopping a deployment via this script causes QC web UI to show "Stopped".

Notes:
  QuantConnect's auth scheme uses a SHA-256 of "{token}:{timestamp}" as the
  password component of HTTP Basic auth, with a "Timestamp" header carrying
  the same Unix timestamp. The server rejects requests if the timestamp is
  more than ~30 seconds out of sync, so make sure your machine clock is NTP-
  synced. If QC has updated their auth scheme since this was written, see:
  https://www.quantconnect.com/docs/v2/api-reference
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time

import requests


QC_API_BASE = "https://www.quantconnect.com/api/v2"


def auth_headers() -> dict:
    """Build the Authorization + Timestamp headers QC expects."""
    user_id = os.environ.get("QC_USER_ID")
    token = os.environ.get("QC_API_TOKEN")
    if not user_id or not token:
        sys.exit(
            "ERROR: set QC_USER_ID and QC_API_TOKEN environment variables.\n"
            "  export QC_USER_ID='123456'\n"
            "  export QC_API_TOKEN='your-api-token-here'"
        )

    ts = str(int(time.time()))
    hashed = hashlib.sha256(f"{token}:{ts}".encode()).hexdigest()
    creds = base64.b64encode(f"{user_id}:{hashed}".encode()).decode()
    return {
        "Authorization": "Basic " + creds,
        "Timestamp": ts,
    }


def call(endpoint: str, payload: dict | None = None) -> dict:
    """POST to a QC v2 API endpoint and return the parsed JSON."""
    url = f"{QC_API_BASE}/{endpoint.lstrip('/')}"
    resp = requests.post(url, json=payload or {}, headers=auth_headers(), timeout=20)
    if resp.status_code != 200:
        sys.exit(f"ERROR: {endpoint} returned HTTP {resp.status_code}\n  body: {resp.text[:500]}")
    data = resp.json()
    if not data.get("success", False):
        # QC sometimes returns 200 OK with success=false and an errors array.
        sys.exit(f"ERROR: {endpoint} returned success=false\n  body: {json.dumps(data, indent=2)}")
    return data


def list_projects() -> list[dict]:
    data = call("projects/read")
    return data.get("projects", [])


def list_live_deployments() -> list[dict]:
    data = call("live/read")
    return data.get("live", [])


def stop_deployment(project_id: int) -> None:
    """Stop the live deployment for a given project ID."""
    call("live/update/stop", {"projectId": project_id})
    print(f"Requested stop for project {project_id}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantConnect dry-run controller (G4).")
    parser.add_argument(
        "--stop",
        type=int,
        metavar="PROJECT_ID",
        help="Stop the live deployment for this project ID.",
    )
    args = parser.parse_args()

    if args.stop is not None:
        stop_deployment(args.stop)
        return

    print("=== QuantConnect Projects ===")
    projects = list_projects()
    if not projects:
        print("  (none)")
    for p in projects:
        print(f"  [{p.get('projectId'):>8}]  {p.get('name')}  (lang={p.get('language')})")

    print("\n=== Live Deployments ===")
    deployments = list_live_deployments()
    if not deployments:
        print("  (none currently running)")
    for d in deployments:
        print(
            f"  project={d.get('projectId'):>8}  "
            f"deployId={d.get('deployId')}  "
            f"brokerage={d.get('brokerage')}  "
            f"status={d.get('status')}"
        )


if __name__ == "__main__":
    main()

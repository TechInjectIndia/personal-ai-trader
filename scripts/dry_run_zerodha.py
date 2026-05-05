"""
Helm dry run — Gate G1.

Goal: prove `pykiteconnect` can place a real order on Zerodha and we can read
the fill back. Buys exactly 1 NIFTYBEES (Nifty 50 ETF) at market, CNC product
(delivery). One unit is ~₹250, so well under any sane risk threshold.

Pass criterion (G1):
  Order shows COMPLETE in BOTH this script's stdout AND your Kite app under
  Orders. If it appears in only one, the SDK is broken or you're hitting the
  wrong account.

Required env:
  KITE_API_KEY       — from your Kite Connect app at developers.kite.trade
  KITE_API_SECRET    — same place
  KITE_ACCESS_TOKEN  — daily token from kite_login.py

Usage:
  python scripts/dry_run_zerodha.py
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: env var {name} is not set.")
    return val


def main() -> int:
    api_key = env("KITE_API_KEY")
    access_token = env("KITE_ACCESS_TOKEN")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Sanity check first — this proves auth works without placing an order.
    try:
        profile = kite.profile()
    except Exception as exc:
        sys.exit(f"G1 — profile() failed (auth issue?): {exc}")
    print(f"G1 — connected as {profile['user_name']} ({profile['email']}) on {profile['broker']}")

    # Place the test order: 1 NIFTYBEES, CNC, market.
    print("G1 — placing 1 NIFTYBEES BUY (CNC, market)")
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol="NIFTYBEES",
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=1,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_MARKET,
            tag="helm-dryrun-g1",
        )
    except Exception as exc:
        sys.exit(f"G1 — place_order failed: {exc}")
    print(f"G1 — order_id: {order_id}")

    # Poll for terminal status (up to 60 seconds).
    print("G1 — waiting for fill...")
    terminal_states = {"COMPLETE", "REJECTED", "CANCELLED"}
    for attempt in range(20):
        time.sleep(3)
        history = kite.order_history(order_id=order_id)
        latest = history[-1]
        status = latest["status"]
        if status in terminal_states:
            qty = latest.get("filled_quantity", 0)
            avg = latest.get("average_price", 0.0)
            print(f"G1 — status: {status}  qty={qty}  avg_price={avg}")
            if status == "COMPLETE":
                print()
                print("✓ G1 PASSED on the script side.")
                print("  Now verify in Kite (kite.zerodha.com → Orders) — order should be visible there too.")
                print("  Optional: sell back the unit so you don't carry a real position from a dry run.")
                return 0
            print()
            print(f"✗ G1 FAILED — order ended in {status}.")
            print(f"  Reason: {latest.get('status_message') or '(none reported)'}")
            return 1

    print("G1 — order didn't reach a terminal state in 60s.")
    print("  Check Kite Orders directly. NSE may be outside market hours (09:15–15:30 IST).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

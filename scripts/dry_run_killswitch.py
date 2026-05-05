"""
Helm dry run — Gate G4.

Goal: prove the kill-switch path works programmatically on both brokers.
This is the most safety-critical gate — without programmatic cancel, we
cannot run autonomously.

What it does:
  1. Places a LIMIT order on each broker, well off-market so it does NOT fill:
       - Zerodha: BUY 1 NIFTYBEES at ₹1 (will rest unfilled)
       - IBKR paper: BUY 1 VOO at $1 (will rest unfilled)
  2. Waits 5 seconds.
  3. Cancels both via kite.cancel_order() and ib.cancelOrder().
  4. Verifies both show CANCELLED.

Pass criterion (G4):
  Both brokers report CANCELLED in BOTH script stdout AND their UIs.

Usage:
  python scripts/dry_run_killswitch.py
"""

import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect
from ib_insync import IB, Stock, LimitOrder, util

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: env var {name} is not set.")
    return val


def zerodha_place_and_cancel() -> bool:
    """Place an off-market limit order on Zerodha, then cancel it."""
    kite = KiteConnect(api_key=env("KITE_API_KEY"))
    kite.set_access_token(env("KITE_ACCESS_TOKEN"))

    print("G4 — Zerodha: placing LIMIT BUY 1 NIFTYBEES @ ₹1 (will rest)")
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol="NIFTYBEES",
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=1,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=1.0,
            tag="helm-dryrun-g4",
        )
    except Exception as exc:
        print(f"G4 — Zerodha place_order failed: {exc}")
        return False
    print(f"G4 — Zerodha order_id: {order_id}")

    time.sleep(3)
    print(f"G4 — cancelling Zerodha {order_id}...")
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
    except Exception as exc:
        print(f"G4 — Zerodha cancel_order failed: {exc}")
        return False

    # Poll until terminal.
    for _ in range(10):
        time.sleep(2)
        history = kite.order_history(order_id=order_id)
        status = history[-1]["status"]
        if status == "CANCELLED":
            print(f"G4 — Zerodha {order_id} → CANCELLED ✓")
            return True
        if status in ("REJECTED", "COMPLETE"):
            print(f"G4 — Zerodha {order_id} ended {status} (not cancelled cleanly)")
            return False
    print(f"G4 — Zerodha {order_id} did not converge to CANCELLED in 20s")
    return False


def ibkr_place_and_cancel() -> bool:
    """Place an off-market limit order on IBKR paper, then cancel it."""
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", 7497))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", 99))
    if port == 7496:
        sys.exit("REFUSING — port 7496 is LIVE. Dry run must use paper.")

    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=15)
    accounts = ib.managedAccounts()
    if not accounts or not accounts[0].startswith("DU"):
        ib.disconnect()
        sys.exit("G4 — IBKR not on paper account. ABORT.")

    contract = Stock("VOO", "SMART", "USD")
    ib.qualifyContracts(contract)
    order = LimitOrder("BUY", 1, lmtPrice=1.0)
    order.account = accounts[0]

    print("G4 — IBKR: placing LIMIT BUY 1 VOO @ $1 (will rest)")
    trade = ib.placeOrder(contract, order)
    ib.sleep(1.0)
    print(f"G4 — IBKR order_id: {trade.order.orderId}")

    time.sleep(3)
    print(f"G4 — cancelling IBKR {trade.order.orderId}...")
    ib.cancelOrder(order)

    for _ in range(10):
        ib.waitOnUpdate(timeout=2)
        status = trade.orderStatus.status
        if status == "Cancelled":
            print(f"G4 — IBKR {trade.order.orderId} → Cancelled ✓")
            ib.disconnect()
            return True
        if status == "Filled":
            print(f"G4 — IBKR {trade.order.orderId} unexpectedly Filled")
            ib.disconnect()
            return False

    print(f"G4 — IBKR {trade.order.orderId} did not converge to Cancelled in 20s")
    ib.disconnect()
    return False


def main() -> int:
    print("G4 — placing test limit orders (will not fill), then cancelling both")
    print()
    z_ok = zerodha_place_and_cancel()
    print()
    i_ok = ibkr_place_and_cancel()
    print()

    if z_ok and i_ok:
        print("✓ G4 PASSED — kill-switch path works on both brokers.")
        print("  Final cross-check: confirm both orders show CANCELLED in their broker UIs.")
        return 0
    print(f"✗ G4 FAILED — Zerodha={'ok' if z_ok else 'FAIL'} IBKR={'ok' if i_ok else 'FAIL'}")
    print("  Do NOT proceed to live deploy until G4 is green on the broker(s) you intend to use.")
    return 1


if __name__ == "__main__":
    util.startLoop()
    raise SystemExit(main())

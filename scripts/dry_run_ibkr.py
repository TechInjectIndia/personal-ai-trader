"""
Helm dry run — Gate G2.

Goal: prove `ib_insync` can place a real order on IBKR paper account.
Buys 1 share of VOO (Vanguard S&P 500 ETF) on the DU… paper account.

Pre-requisite:
  IB Gateway (or TWS) is running locally with API enabled.
  Default paper port: 7497. Live port: 7496. We use paper.

Pass criterion (G2):
  Fill appears in BOTH this script's stdout AND your IBKR Client Portal under
  Trades.

Required env (optional — defaults to paper account on localhost):
  IBKR_HOST          — default 127.0.0.1
  IBKR_PORT          — default 7497 (paper). Use 7496 for live (NEVER for dry run).
  IBKR_CLIENT_ID     — default 99 (any unique int between 1–32767)

Usage:
  python scripts/dry_run_ibkr.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from ib_insync import IB, Stock, MarketOrder, util

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> int:
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", 7497))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", 99))

    if port == 7496:
        sys.exit("REFUSING — port 7496 is LIVE. Dry run must use paper (7497).")

    ib = IB()
    print(f"G2 — connecting to IB Gateway {host}:{port} (clientId={client_id})")
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
    except Exception as exc:
        sys.exit(f"G2 — connect failed: {exc}\n  Is IB Gateway running and API enabled?")

    # Confirm paper account.
    accounts = ib.managedAccounts()
    if not accounts:
        ib.disconnect()
        sys.exit("G2 — no managed accounts returned.")
    account = accounts[0]
    if not account.startswith("DU"):
        ib.disconnect()
        sys.exit(f"G2 — account {account} is not a paper (DU…) account. ABORT.")
    print(f"G2 — connected to paper account {account}")

    # Use delayed-frozen data so the request works even without a live data subscription.
    ib.reqMarketDataType(3)

    # Place the test order: 1 VOO, market.
    contract = Stock("VOO", "SMART", "USD")
    ib.qualifyContracts(contract)
    order = MarketOrder("BUY", 1)
    order.account = account

    print("G2 — placing 1 VOO BUY (market)")
    trade = ib.placeOrder(contract, order)
    print(f"G2 — order_id: {trade.order.orderId}")

    print("G2 — waiting for fill (up to 60s)...")
    deadline = ib.client.connectionTime() + 60.0
    while not trade.isDone() and ib.client.connectionTime() < deadline:
        ib.waitOnUpdate(timeout=3)

    status = trade.orderStatus.status
    filled = trade.orderStatus.filled
    avg = trade.orderStatus.avgFillPrice

    print(f"G2 — status: {status}  qty={filled}  avg_price={avg}")
    ib.disconnect()

    if status == "Filled":
        print()
        print("✓ G2 PASSED on the script side.")
        print("  Now verify in IBKR Client Portal → Trades — order should be visible there too.")
        return 0

    print()
    print(f"✗ G2 incomplete — order ended in {status}.")
    print("  Outside US market hours (09:30–16:00 ET)? Order may sit until next open. Check Client Portal.")
    return 1


if __name__ == "__main__":
    util.startLoop()  # idempotent if already running
    raise SystemExit(main())

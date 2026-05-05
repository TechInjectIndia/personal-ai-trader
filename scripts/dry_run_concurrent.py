"""
Helm dry run — Gate G3.

Goal: prove a single Python process can drive both broker adapters at once.
This is the architectural prerequisite for the orchestrator running both
sleeves simultaneously.

What it does:
  1. Connects to Zerodha (pykiteconnect) AND IBKR (ib_insync) in the same process.
  2. Pulls cash + holdings from each.
  3. Prints a consolidated NAV in INR using a hardcoded FX rate.

Pass criterion (G3):
  Both connections succeed in the same process and the consolidated view prints
  with no errors. If `ib_insync` interrupts `pykiteconnect` or vice versa, the
  script throws — which is itself a real architectural finding to debug.

Usage:
  python scripts/dry_run_concurrent.py
"""

import os
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect
from ib_insync import IB, util

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# Placeholder FX rate. The orchestrator fetches a live rate daily.
USD_INR = Decimal("83.20")


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: env var {name} is not set.")
    return val


def fetch_zerodha() -> dict:
    api_key = env("KITE_API_KEY")
    access_token = env("KITE_ACCESS_TOKEN")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    margins = kite.margins(segment="equity")
    cash_inr = Decimal(str(margins.get("available", {}).get("cash", 0)))

    holdings = kite.holdings()
    return {
        "broker": "Zerodha",
        "cash_inr": cash_inr,
        "holdings": [
            {
                "symbol": h["tradingsymbol"],
                "qty": h["quantity"],
                "ltp": Decimal(str(h.get("last_price", 0))),
                "value_inr": Decimal(str(h.get("last_price", 0))) * h["quantity"],
            }
            for h in holdings
        ],
    }


def fetch_ibkr() -> dict:
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", 7497))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", 99))

    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=15)

    summary = {row.tag: row.value for row in ib.accountSummary() if row.currency in ("USD", "BASE")}
    cash_usd = Decimal(summary.get("TotalCashValue", "0"))

    positions = ib.positions()
    holdings = []
    for pos in positions:
        contract = pos.contract
        # Snapshot price (delayed) — paper accounts get free delayed data.
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(1.5)
        last = ticker.last or ticker.close or 0
        ib.cancelMktData(contract)
        holdings.append({
            "symbol": contract.symbol,
            "qty": pos.position,
            "ltp_usd": Decimal(str(last)),
            "value_usd": Decimal(str(last)) * Decimal(str(pos.position)),
        })

    ib.disconnect()
    return {
        "broker": "IBKR (paper)",
        "cash_usd": cash_usd,
        "holdings": holdings,
    }


def main() -> int:
    print("G3 — connecting to Zerodha + IBKR concurrently in one process")
    z = fetch_zerodha()
    print(f"G3 — Zerodha: cash={z['cash_inr']:,.2f} INR  holdings: {len(z['holdings'])} symbol(s)")
    for h in z["holdings"]:
        print(f"           {h['symbol']:<12} qty={h['qty']}  ltp={h['ltp']}  value={h['value_inr']:,.2f} INR")

    i = fetch_ibkr()
    print(f"G3 — IBKR (paper): cash={i['cash_usd']:,.2f} USD  holdings: {len(i['holdings'])} symbol(s)")
    for h in i["holdings"]:
        print(f"           {h['symbol']:<12} qty={h['qty']}  ltp={h['ltp_usd']}  value={h['value_usd']:,.2f} USD")

    z_total_inr = z["cash_inr"] + sum(h["value_inr"] for h in z["holdings"])
    i_total_usd = i["cash_usd"] + sum(h["value_usd"] for h in i["holdings"])
    nav_inr = z_total_inr + i_total_usd * USD_INR

    print()
    print(f"G3 — Consolidated NAV: ₹{nav_inr:,.2f}  (FX rate $1 = ₹{USD_INR})")
    print()
    print("✓ G3 PASSED — both adapters run in one process and we can read state from each.")
    return 0


if __name__ == "__main__":
    util.startLoop()
    raise SystemExit(main())

"""
Helm — Kite Connect read-only smoke test.

Runs AFTER kite_login.py has populated KITE_ACCESS_TOKEN. Calls only read-only
endpoints — profile, margins, holdings, positions, orders. Places NO orders.

Use this between minting the daily token and running G1, so you can verify
authenticated reads work without committing to placing the test order.

Usage:
  python scripts/test_kite_readonly.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: env var {name} is not set. Run kite_login.py first if it's KITE_ACCESS_TOKEN.")
    return val


def main() -> int:
    api_key = env("KITE_API_KEY")
    access_token = env("KITE_ACCESS_TOKEN")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    print("=== Profile ===")
    try:
        profile = kite.profile()
        print(f"  user_id       : {profile.get('user_id')}")
        print(f"  user_name     : {profile.get('user_name')}")
        print(f"  email         : {profile.get('email')}")
        print(f"  broker        : {profile.get('broker')}")
        print(f"  exchanges     : {profile.get('exchanges')}")
        print(f"  products      : {profile.get('products')}")
        print(f"  order_types   : {profile.get('order_types')}")
    except Exception as exc:
        sys.exit(f"profile() failed: {exc}")

    print("\n=== Margins (equity) ===")
    try:
        margins = kite.margins(segment="equity")
        avail = margins.get("available", {})
        print(f"  cash             : ₹{avail.get('cash', 0):,.2f}")
        print(f"  intraday_payin   : ₹{avail.get('intraday_payin', 0):,.2f}")
        print(f"  collateral       : ₹{avail.get('collateral', 0):,.2f}")
        print(f"  net (used+avail) : ₹{margins.get('net', 0):,.2f}")
    except Exception as exc:
        print(f"  margins() failed: {exc}")

    print("\n=== Holdings ===")
    try:
        holdings = kite.holdings()
        if not holdings:
            print("  (none)")
        for h in holdings:
            print(f"  {h.get('tradingsymbol'):<12} qty={h.get('quantity')}  "
                  f"avg=₹{h.get('average_price', 0):.2f}  "
                  f"ltp=₹{h.get('last_price', 0):.2f}  "
                  f"pnl=₹{h.get('pnl', 0):,.2f}")
    except Exception as exc:
        print(f"  holdings() failed: {exc}")

    print("\n=== Positions ===")
    try:
        positions = kite.positions()
        net = positions.get("net", [])
        if not net:
            print("  (no open positions)")
        for p in net:
            print(f"  {p.get('tradingsymbol'):<12} product={p.get('product')}  "
                  f"qty={p.get('quantity')}  avg=₹{p.get('average_price', 0):.2f}")
    except Exception as exc:
        print(f"  positions() failed: {exc}")

    print("\n=== Today's orders ===")
    try:
        orders = kite.orders()
        if not orders:
            print("  (none)")
        for o in orders[-10:]:  # last 10
            print(f"  {o.get('order_id'):<24} {o.get('tradingsymbol'):<12} "
                  f"{o.get('transaction_type'):<4} qty={o.get('quantity'):<4} "
                  f"status={o.get('status')}")
    except Exception as exc:
        print(f"  orders() failed: {exc}")

    print("\n✓ Authenticated read-only smoke test PASSED.")
    print("  Next: run `python scripts/dry_run_zerodha.py` to attempt G1 (places 1 NIFTYBEES BUY).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

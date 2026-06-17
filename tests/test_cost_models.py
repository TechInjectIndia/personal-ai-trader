"""M4 — per-market cost models (pure)."""

from __future__ import annotations

from decimal import Decimal

from helm import charges
from helm.markets.costs import AlpacaEquityCosts, CryptoBpsCosts, ZerodhaCosts


def test_zerodha_matches_canonical_charges():
    z = ZerodhaCosts()
    for side in ("BUY", "SELL"):
        a = z.round_trip_breakdown(side, Decimal("10"), Decimal("100"), Decimal("101"))
        b = charges.round_trip_breakdown(side, Decimal("10"), Decimal("100"), Decimal("101"))
        assert a.as_dict() == b.as_dict()


def test_us_equity_zero_commission_sec_taf_only():
    c = AlpacaEquityCosts()
    bd = c.round_trip_breakdown("BUY", Decimal("100"), Decimal("200"), Decimal("201"))
    assert bd.brokerage == Decimal("0.00")     # $0 commission
    assert bd.stt == Decimal("0.00")
    assert bd.gst == Decimal("0.00")
    # SEC on sell notional (201*100*0.0000278) + TAF (100*0.000166), to the cent.
    assert bd.sebi == Decimal("0.5588")
    assert bd.exchange_txn == Decimal("0.0166")
    assert bd.total == Decimal("0.58")


def test_us_equity_taf_capped():
    c = AlpacaEquityCosts()
    bd = c.round_trip_breakdown("BUY", Decimal("1000000"), Decimal("10"), Decimal("10"))
    assert bd.exchange_txn == Decimal("8.30")  # FINRA TAF per-trade cap


def test_crypto_bps_both_legs_no_tax():
    c = CryptoBpsCosts()  # default 0.10% taker
    bd = c.round_trip_breakdown("BUY", Decimal("0.01"), Decimal("60000"), Decimal("60500"))
    # (600 + 605) * 0.0010 = 1.205 → 1.20 (banker's rounding to the cent)
    assert bd.total == Decimal("1.20")
    assert bd.brokerage == bd.total
    assert bd.stt == Decimal("0.00") and bd.gst == Decimal("0.00")


def test_crypto_bps_configurable_rate():
    c = CryptoBpsCosts(taker_bps=Decimal("0.00075"))  # 7.5 bps (BNB discount)
    bd = c.round_trip_breakdown("SELL", Decimal("1"), Decimal("100"), Decimal("100"))
    assert bd.total == Decimal("0.15")  # (100 + 100) * 0.00075

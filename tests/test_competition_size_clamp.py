"""Freestyle sizing: an agent's requested qty is CLAMPED to the affordable
budget, not hard-rejected.

Before this fix, `_size_qty` returned `requested_qty` verbatim and
`risk.evaluate` then hard-rejected any order whose notional exceeded the
per-trade cap / wallet — discarding the whole trade. That was the dominant
cause of cap-blocked SKIPs (gemini-momentum/nemotron-trend got ~0 fills on
hundreds of valid intents). Now an over-cap request sizes DOWN to what fits.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import helm.competition.execute as ex


def _wallet_stub(available: str, realised: str = "0"):
    # _size_qty only reads .available and .realised_net_pnl.
    return SimpleNamespace(
        available=Decimal(available), realised_net_pnl=Decimal(realised)
    )


@pytest.fixture
def _wallet(monkeypatch):
    """Pin a competitor wallet at ₹50k free, 0 realised, base cap ₹15k."""
    w = _wallet_stub("50000")
    monkeypatch.setattr(ex, "competitor_wallet_state", lambda cid, market="IN": w)
    return w


def test_oversized_request_clamps_to_cap(_wallet):
    # 10 shares of a ₹2150 name = ₹21,500 > ₹15k cap → clamp to floor(15000/2150)=6
    qty = ex._size_qty("gemini-momentum", Decimal("2150"), requested_qty=10)
    assert qty == 6
    assert qty * Decimal("2150") <= Decimal("15000")


def test_request_within_cap_is_untouched(_wallet):
    # 4 shares @ ₹2150 = ₹8,600 < cap → take the agent's request as-is
    assert ex._size_qty("gemini-momentum", Decimal("2150"), requested_qty=4) == 4


def test_no_request_auto_sizes_to_budget(_wallet):
    # No explicit qty → size to the affordable share count (cap-bound here)
    assert ex._size_qty("gemini-momentum", Decimal("2150"), requested_qty=None) == 6


def test_unaffordable_price_returns_zero(_wallet):
    # One share costs more than the cap → 0 (clean skip, never negative)
    assert ex._size_qty("gemini-momentum", Decimal("20000"), requested_qty=5) == 0


def test_wallet_smaller_than_cap_binds(monkeypatch):
    w = _wallet_stub("4000")                # only ₹4k free
    monkeypatch.setattr(ex, "competitor_wallet_state", lambda cid, market="IN": w)
    # request 10 @ ₹1000 = ₹10k, but wallet caps at ₹4k → 4 shares
    assert ex._size_qty("gemini-momentum", Decimal("1000"), requested_qty=10) == 4

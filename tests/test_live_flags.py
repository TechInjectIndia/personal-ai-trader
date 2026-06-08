"""Settings-backed feature-flag + tunable resolver (dashboard Control Center).

The critical safety property: with NO settings override, live_flag/live_tunable
equal the code constant (behaviour byte-identical to today). Overrides take
effect at the next read. Uses the real flag keys, so it cleans up after itself.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from helm import config
from helm.data.store import conn, set_setting

_KEYS = ("CONVICTION_SIZING_ENABLED", "CONTEXT_SIGNALS_ENABLED",
         "HOUSE_STRATEGY_KEYED_SLOTS", "MIN_EDGE_TO_COST")


def _purge() -> None:
    with conn() as c:
        c.execute("DELETE FROM settings WHERE key = ANY(%s)", (list(_KEYS),))


@pytest.fixture(autouse=True)
def _scrub():
    _purge()
    yield
    _purge()


def test_flags_default_to_code_constants():
    # No override → identical to the module constants (the safety guarantee).
    assert config.live_flag("CONVICTION_SIZING_ENABLED") is config.CONVICTION_SIZING_ENABLED
    assert config.live_flag("CONTEXT_SIGNALS_ENABLED") is config.CONTEXT_SIGNALS_ENABLED
    assert config.live_flag("HOUSE_STRATEGY_KEYED_SLOTS") is config.HOUSE_STRATEGY_KEYED_SLOTS


def test_tunables_default_to_code_constants():
    assert config.live_tunable("MIN_EDGE_TO_COST") == config.MIN_EDGE_TO_COST
    assert config.live_tunable("CONVICTION_FLOOR") == config.CONVICTION_FLOOR


def test_flag_override_true_then_false():
    set_setting("CONVICTION_SIZING_ENABLED", True, actor="test")
    assert config.live_flag("CONVICTION_SIZING_ENABLED") is True
    set_setting("CONVICTION_SIZING_ENABLED", False, actor="test")
    assert config.live_flag("CONVICTION_SIZING_ENABLED") is False


def test_flag_override_accepts_string_forms():
    set_setting("HOUSE_STRATEGY_KEYED_SLOTS", "true", actor="test")
    assert config.live_flag("HOUSE_STRATEGY_KEYED_SLOTS") is True


def test_tunable_override():
    set_setting("MIN_EDGE_TO_COST", 5.0, actor="test")
    assert config.live_tunable("MIN_EDGE_TO_COST") == Decimal("5.0")

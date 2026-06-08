"""Bot-side context_client + decide_signals integration tests.

These guard the live-trade-path invariants:
  1. Flag unset => get_context returns None AND _build_user_prompt is
     BYTE-IDENTICAL to today (no external_context key) = "changes nothing when
     flag unset" regression guard.
  2. Non-200 / timeout / garbage body / stale => None + fail_open|miss logged
     (fail-open, never raises).
  3. Happy path => external_context injected into the prompt + hit logged.

Hermetic: no network. We monkeypatch urllib.request.urlopen. Do NOT model this
on the stale tests/test_risk.py.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from helm import context_client
from scripts import decide_signals

IST = ZoneInfo("Asia/Kolkata")


class _FakeResp:
    """Context-manager stand-in for the object urlopen returns."""

    def __init__(self, body: bytes, status: int = 200):
        self._buf = io.BytesIO(body)
        self.status = status

    def getcode(self):
        return self.status

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, fn):
    monkeypatch.setattr(context_client.urllib.request, "urlopen", fn)


def _no_dotenv(monkeypatch):
    """Stop load_dotenv from reading a real .env and setting the flag."""
    monkeypatch.setattr(context_client, "load_dotenv", lambda *a, **k: None, raising=False)


# --- sample signal (mirrors a signals row shape used by _build_user_prompt) ---
def _sample_sig() -> dict:
    return {
        "id": 7,
        "ts": datetime(2026, 6, 8, 10, 0, tzinfo=IST),
        "strategy": "orb",
        "symbol": "RELIANCE",
        "side": "BUY",
        "entry_price": Decimal("1400.00"),
        "stop_loss": Decimal("1390.00"),
        "target": Decimal("1420.00"),
        "rationale": "ORB breakout",
        "payload": {"range_high": 1399.5},
    }


# ---------------------------------------------------------------------------
# 1. Flag unset: get_context None + prompt byte-identical
# ---------------------------------------------------------------------------
def test_flag_unset_returns_none(monkeypatch):
    monkeypatch.delenv("CONTEXT_ENGINE_URL", raising=False)
    monkeypatch.setattr(context_client.os.environ, "get", lambda *a, **k: None, raising=False)
    # Ensure load_dotenv doesn't smuggle in a value from a real .env.
    monkeypatch.setattr(context_client, "load_dotenv", lambda *a, **k: None, raising=False)
    # And urlopen must never be called when the flag is off.
    def _boom(*a, **k):  # pragma: no cover - asserts it's not reached
        raise AssertionError("urlopen called while flag unset")
    _patch_urlopen(monkeypatch, _boom)

    assert context_client.get_context("RELIANCE", signal_id=7) is None


def test_prompt_byte_identical_when_no_context():
    sig = _sample_sig()
    candles: list[dict] = []
    snap = {"open_positions": 0}
    hist: list[dict] = []

    baseline = decide_signals._build_user_prompt(sig, candles, snap, hist)
    with_none = decide_signals._build_user_prompt(sig, candles, snap, hist, context=None)

    assert baseline == with_none
    assert "external_context" not in baseline


# ---------------------------------------------------------------------------
# 2. Fail-open paths
# ---------------------------------------------------------------------------
def _enable_flag(monkeypatch):
    monkeypatch.setenv("CONTEXT_ENGINE_URL", "http://127.0.0.1:8601")


def test_non_200_fails_open(monkeypatch):
    _enable_flag(monkeypatch)
    _no_dotenv(monkeypatch)
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResp(b"{}", status=503))
    assert context_client.get_context("RELIANCE") is None


def test_timeout_fails_open(monkeypatch):
    _enable_flag(monkeypatch)
    _no_dotenv(monkeypatch)

    def _raise(*a, **k):
        raise TimeoutError("slow")

    _patch_urlopen(monkeypatch, _raise)
    assert context_client.get_context("RELIANCE") is None


def test_urlerror_fails_open(monkeypatch):
    _enable_flag(monkeypatch)
    _no_dotenv(monkeypatch)

    def _raise(*a, **k):
        raise context_client.urllib.error.URLError("refused")

    _patch_urlopen(monkeypatch, _raise)
    assert context_client.get_context("RELIANCE") is None


def test_garbage_body_fails_open(monkeypatch):
    _enable_flag(monkeypatch)
    _no_dotenv(monkeypatch)
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResp(b"not json{{{"))
    assert context_client.get_context("RELIANCE") is None


def test_stale_response_is_miss(monkeypatch):
    _enable_flag(monkeypatch)
    _no_dotenv(monkeypatch)
    body = json.dumps({"symbol": "RELIANCE", "score": 0.0, "rationale": "",
                       "stale": True}).encode()
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResp(body))
    assert context_client.get_context("RELIANCE") is None


# ---------------------------------------------------------------------------
# 3. Happy path: returns ctx and injects into the prompt
# ---------------------------------------------------------------------------
def test_happy_path_returns_ctx(monkeypatch):
    _enable_flag(monkeypatch)
    _no_dotenv(monkeypatch)
    body = json.dumps({
        "symbol": "RELIANCE",
        "score": -0.412,
        "rationale": "Q4 miss; guidance cut.",
        "as_of": "2026-06-08T11:32:14+05:30",
        "half_life_min": 90,
        "stale": False,
    }).encode()
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResp(body))

    ctx = context_client.get_context("RELIANCE", signal_id=7)
    assert ctx == {"score": -0.412, "rationale": "Q4 miss; guidance cut."}


def test_happy_path_injects_into_prompt():
    sig = _sample_sig()
    ctx = {"score": -0.412, "rationale": "Q4 miss; guidance cut."}
    prompt = decide_signals._build_user_prompt(sig, [], {"open_positions": 0}, [], context=ctx)
    assert "external_context" in prompt
    assert "Q4 miss" in prompt


def test_get_context_never_raises(monkeypatch):
    """Even a wildly broken urlopen must yield None, never propagate."""
    _enable_flag(monkeypatch)
    _no_dotenv(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("anything")

    _patch_urlopen(monkeypatch, _raise)
    assert context_client.get_context("RELIANCE") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

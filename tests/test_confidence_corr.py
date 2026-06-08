"""F5 measurement gate tests — confidence ↔ outcome correlation (read-only).

Two layers:
1. Pure parse/bucketing tests against synthetic `decisions.reasoning` strings
   (no DB) — fast, deterministic, exercise the exact tag format the decider
   writes at scripts/decide_signals.py:298.
2. A DB smoke that just calls confidence_outcome_correlation() against the live
   Postgres and asserts the shape is sane (skipped if no DB / no data).
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from helm.analytics.economics import (
    _CONF_BANDS,
    ConfidenceOutcome,
    confidence_outcome_correlation,
)

# Same format the decider emits: `[{source} {mode}/{model} conf={confidence:.2f}] ...`
_PY_CONF_RE = re.compile(r"conf=([01]\.\d{2})")


def _parse(reasoning: str) -> Decimal | None:
    m = _PY_CONF_RE.search(reasoning)
    return Decimal(m.group(1)) if m else None


def _band_for(conf: Decimal) -> str | None:
    for i, (band, lo, hi) in enumerate(_CONF_BANDS):
        if i == 0:
            if lo <= conf <= hi:
                return band
        elif lo < conf <= hi:
            return band
    return None


# ── 1. Pure parse tests ──────────────────────────────────────────────────────

def test_parse_real_tag():
    s = "[scan_signals cli/claude-sonnet-4-6 conf=0.75] Clean gap-fade setup"
    assert _parse(s) == Decimal("0.75")


def test_parse_blocked_suffix_preserved():
    # paper_execute appends ` [BLOCKED: ...]` but the conf= prefix survives.
    s = "[scan_signals cli/claude-sonnet-4-6 conf=0.62] reasons [BLOCKED: min edge]"
    assert _parse(s) == Decimal("0.62")


def test_parse_manual_run_has_no_conf():
    # Manual paper_execute.py runs pass reasoning="" — no conf tag, excluded.
    assert _parse("") is None
    assert _parse("manual override, no confidence here") is None


def test_parse_boundary_values():
    assert _parse("conf=0.00] x") == Decimal("0.00")
    assert _parse("conf=1.00] x") == Decimal("1.00")


# ── 2. Bucketing tests (band assignment matches the SQL CASE in economics) ─────

@pytest.mark.parametrize("conf,expected", [
    (Decimal("0.50"), "<0.55"),
    (Decimal("0.54"), "<0.55"),
    (Decimal("0.55"), "<0.55"),     # closed-right on first band
    (Decimal("0.56"), "0.55–0.65"),
    (Decimal("0.65"), "0.55–0.65"),
    (Decimal("0.66"), "0.65–0.75"),
    (Decimal("0.75"), "0.65–0.75"),
    (Decimal("0.80"), "0.75–0.85"),
    (Decimal("0.85"), "0.75–0.85"),
    (Decimal("0.90"), "≥0.85"),
    (Decimal("1.00"), "≥0.85"),
])
def test_band_assignment(conf, expected):
    assert _band_for(conf) == expected


def test_bands_cover_full_range_no_gaps():
    # Every conf in [0,1] at 2-decimal granularity lands in exactly one band.
    for i in range(0, 101):
        conf = Decimal(i) / Decimal("100")
        assert _band_for(conf) is not None, conf


# ── 3. Win% / net-expectancy hand-computed fixture ─────────────────────────────

def test_bucket_math_hand_computed():
    # Synthetic 0.65–0.75 bucket: 3 trades, nets +100, -40, +20.
    nets = [Decimal("100"), Decimal("-40"), Decimal("20")]
    n = len(nets)
    wins = sum(1 for x in nets if x > 0)
    win_pct = (Decimal(wins) / Decimal(n) * 100).quantize(Decimal("0.01"))
    expectancy = (sum(nets) / Decimal(n)).quantize(Decimal("0.01"))
    assert win_pct == Decimal("66.67")
    assert expectancy == Decimal("26.67")


# ── 4. DB smoke (read-only) ────────────────────────────────────────────────────

def test_db_smoke_shape():
    try:
        co = confidence_outcome_correlation()
    except Exception as exc:  # no DB available in this env → skip
        pytest.skip(f"no DB: {exc}")
    assert isinstance(co, ConfidenceOutcome)
    assert co.n >= 0
    # One bucket per defined band, in order.
    assert tuple(b.band for b in co.buckets) == tuple(b[0] for b in _CONF_BANDS)
    assert sum(b.n for b in co.buckets) == co.n  # every conf-tagged trade is bucketed
    if co.corr is not None:
        assert Decimal("-1") <= co.corr <= Decimal("1")
    d = co.as_dict()
    assert set(d) == {"n", "corr", "verdict", "buckets"}

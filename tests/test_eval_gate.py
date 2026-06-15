"""Eval-gate (G3.2): regression-guard logic + Tester stage wiring.

The gate's I/O (replay + baseline load/save) is injected, so these run with no
DB and no model. The Tester stage is tested for the flag-off no-op and the
HOLD→fail path with the gate monkeypatched.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import helm.agents.tester as tester
from helm.eval import gate
from helm.eval.replay import simulate_trade

IST = ZoneInfo("Asia/Kolkata")


def _outcome(net_close: float, *, entry=100.0, qty=10):
    """A closed SimOutcome whose net is driven by the exit close."""
    base = datetime(2026, 6, 10, 9, 30, tzinfo=IST)
    candles = [{"bar_ts": base + timedelta(minutes=1), "close": Decimal(str(net_close))}]
    # target far above so the single candle just marks-to-... use stop/target to force close
    side = "BUY"
    stop = Decimal("0.01")
    target = Decimal(str(net_close))   # close hits target exactly → TARGET exit
    return simulate_trade(side, Decimal(str(entry)), stop, target, qty, candles,
                          apply_ratchet=False)


def _book(*closes):
    return [_outcome(c) for c in closes]


# ── gate verdicts ─────────────────────────────────────────────────────

def test_first_run_establishes_baseline_and_abstains():
    saved = {}
    out = gate.evaluate(
        replay_fn=lambda: _book(104, 103, 105, 104, 106),   # 5 winners
        load_baseline=lambda: None,
        save_baseline=lambda snap: saved.update(snap),
    )
    assert out.verdict == "ABSTAIN"
    assert "baseline established" in out.detail
    assert saved  # baseline was persisted


def test_regression_holds():
    # baseline net high; candidate net much lower → HOLD
    baseline = {"net": "500", "max_drawdown": "10", "closed": 5}
    out = gate.evaluate(
        replay_fn=lambda: _book(101, 100.5, 101, 100.5, 101),  # small gains
        load_baseline=lambda: baseline,
        save_baseline=lambda snap: None,
    )
    assert out.verdict == "HOLD"


def test_clean_pass_advances_baseline():
    baseline = {"net": "10", "max_drawdown": "50", "closed": 5}
    saved = {}
    out = gate.evaluate(
        replay_fn=lambda: _book(106, 105, 107, 106, 108),   # strong gains > baseline
        load_baseline=lambda: baseline,
        save_baseline=lambda snap: saved.update(snap),
    )
    assert out.verdict == "PASS"
    assert saved  # baseline advanced on a clean pass


def test_too_few_trades_abstains():
    out = gate.evaluate(
        replay_fn=lambda: _book(104, 103),   # only 2 trades < min_trades
        load_baseline=lambda: {"net": "0", "max_drawdown": "0", "closed": 5},
        save_baseline=lambda snap: None,
    )
    assert out.verdict == "ABSTAIN"
    assert "not enough" in out.detail


def test_gate_outcome_ok_property():
    base = {"net": "10", "max_drawdown": "50", "closed": 5}
    p = gate.evaluate(replay_fn=lambda: _book(106, 106, 106, 106, 106),
                      load_baseline=lambda: base, save_baseline=lambda s: None)
    assert p.ok is True            # PASS
    h = gate.evaluate(replay_fn=lambda: _book(100.2, 100.2, 100.2, 100.2, 100.2),
                      load_baseline=lambda: {"net": "9999", "max_drawdown": "0", "closed": 5},
                      save_baseline=lambda s: None)
    assert h.verdict == "HOLD" and h.ok is False


# ── Tester stage wiring ───────────────────────────────────────────────

def test_stage_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr("helm.config.live_flag", lambda name: False)
    ok, excerpt, reason = tester._stage_eval_gate()
    assert ok is True and reason is None
    assert "disabled" in excerpt


def test_stage_fails_on_hold(monkeypatch):
    monkeypatch.setattr("helm.config.live_flag", lambda name: name == "EVAL_GATE_ENABLED")

    class _Out:
        verdict = "HOLD"
        detail = "regression: net ₹-100 vs baseline ₹200"
    monkeypatch.setattr("helm.eval.gate.evaluate", lambda **k: _Out())
    ok, excerpt, reason = tester._stage_eval_gate()
    assert ok is False
    assert "HOLD" in reason


def test_stage_passes_on_pass(monkeypatch):
    monkeypatch.setattr("helm.config.live_flag", lambda name: name == "EVAL_GATE_ENABLED")

    class _Out:
        verdict = "PASS"
        detail = "no regression"
    monkeypatch.setattr("helm.eval.gate.evaluate", lambda **k: _Out())
    ok, excerpt, reason = tester._stage_eval_gate()
    assert ok is True and reason is None


def test_stage_abstains_on_engine_error(monkeypatch):
    monkeypatch.setattr("helm.config.live_flag", lambda name: name == "EVAL_GATE_ENABLED")

    def _boom(**k):
        raise RuntimeError("replay blew up")
    monkeypatch.setattr("helm.eval.gate.evaluate", _boom)
    ok, excerpt, reason = tester._stage_eval_gate()
    assert ok is True and reason is None      # never block a release on a gate bug
    assert "abstained on error" in excerpt

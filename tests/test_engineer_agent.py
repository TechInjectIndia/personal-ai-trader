"""
Tests for the Product-Engineer agent's mutator surface.

We use two strategies:

1. Pure string-transform tests for the text-shaped mutators
   (prompt_tweak, param_change, add_filter, bug_fix). These exercise the
   anchor-uniqueness contract without spinning up a git repo.

2. A tmp_path + monkey-patched ``helm.agents.base.REPO_ROOT`` fixture for
   ``process_one_task`` end-to-end, with the DB layer and git ops stubbed.

The full happy path stubs ``claim_next_task`` / ``complete_task`` /
``record_release`` / ``record_run`` / ``git_commit_all`` etc. so the test
runs without a Postgres instance.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from helm.agents import engineer


# ─── helpers ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point REPO_ROOT at a fresh tmp dir laid out like the real repo."""
    (tmp_path / "helm" / "strategies" / "intraday").mkdir(parents=True)
    (tmp_path / "scripts").mkdir(parents=True)
    monkeypatch.setattr("helm.agents.base.REPO_ROOT", tmp_path)
    return tmp_path


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ─── prompt_tweak ─────────────────────────────────────────────────────

PROMPT_FILE_BODY = '''"""Stub of scripts/decide_signals.py for testing."""

SYSTEM_PROMPT = """You are the decider.

Decision principles, in priority order:

1. Respect the risk envelope.

2. Confirm the signal with the recent-candles tape.

OUTPUT FORMAT — strict JSON.
"""
'''


def test_prompt_tweak_append(tmp_repo: Path) -> None:
    target = tmp_repo / "scripts" / "decide_signals.py"
    _write(target, PROMPT_FILE_BODY)
    spec = {
        "file": "scripts/decide_signals.py",
        "anchor": "1. Respect the risk envelope.",
        "action": "append",
        "text": "1a. Also avoid taking trades during the lunch lull (12:00–13:00 IST).",
    }
    res = engineer._mut_prompt_tweak(spec)
    assert res.ok, res.error
    assert "files_touched" in vars(res)
    body = target.read_text()
    assert "1a. Also avoid taking trades during the lunch lull" in body
    # The append should land BEFORE principle 2 (i.e. in the same paragraph).
    idx_new = body.index("1a. Also avoid")
    idx_p2 = body.index("2. Confirm the signal")
    assert idx_new < idx_p2


def test_prompt_tweak_replace(tmp_repo: Path) -> None:
    target = tmp_repo / "scripts" / "decide_signals.py"
    _write(target, PROMPT_FILE_BODY)
    spec = {
        "file": "scripts/decide_signals.py",
        "anchor": "1. Respect the risk envelope.",
        "action": "replace",
        "text": "1. Respect the risk envelope AND the lunch lull.",
    }
    res = engineer._mut_prompt_tweak(spec)
    assert res.ok, res.error
    body = target.read_text()
    assert "1. Respect the risk envelope AND the lunch lull." in body
    assert "1. Respect the risk envelope.\n" not in body


def test_prompt_tweak_anchor_zero_matches(tmp_repo: Path) -> None:
    target = tmp_repo / "scripts" / "decide_signals.py"
    _write(target, PROMPT_FILE_BODY)
    res = engineer._mut_prompt_tweak({
        "file": "scripts/decide_signals.py",
        "anchor": "this string does not appear",
        "action": "append",
        "text": "...",
    })
    assert not res.ok
    assert "0" in res.summary or "0" in (res.error or "")


def test_prompt_tweak_anchor_ambiguous(tmp_repo: Path) -> None:
    body = PROMPT_FILE_BODY + "\n# repeat anchor\nDecision principles, in priority order:\n"
    target = tmp_repo / "scripts" / "decide_signals.py"
    _write(target, body)
    res = engineer._mut_prompt_tweak({
        "file": "scripts/decide_signals.py",
        "anchor": "Decision principles, in priority order:",
        "action": "append",
        "text": "...",
    })
    assert not res.ok
    assert "2" in (res.error or "")


def test_prompt_tweak_rejects_non_prompt_file(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "config.py"
    _write(target, "FOO = 1\n")
    res = engineer._mut_prompt_tweak({
        "file": "helm/config.py",
        "anchor": "FOO = 1",
        "action": "replace",
        "text": "FOO = 2",
    })
    assert not res.ok
    assert "prompt set" in res.summary or "prompt" in (res.error or "")


# ─── param_change ─────────────────────────────────────────────────────

def test_param_change_success(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "config.py"
    _write(target, "FOO = 42  # the answer\nBAR = 7\n")
    res = engineer._mut_param_change({
        "file": "helm/config.py",
        "symbol": "FOO",
        "new_value": 99,
        "expected_type": "int",
    })
    assert res.ok, res.error
    body = target.read_text()
    assert "FOO = 99" in body
    assert "# the answer" in body  # inline comment preserved
    assert "BAR = 7" in body


def test_param_change_decimal(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "config.py"
    _write(target, 'PRICE_CAP: Decimal = Decimal("15000")\n')
    res = engineer._mut_param_change({
        "file": "helm/config.py",
        "symbol": "PRICE_CAP",
        "new_value": "20000",
        "expected_type": "decimal",
    })
    assert res.ok, res.error
    assert 'PRICE_CAP: Decimal = Decimal("20000")' in target.read_text()


def test_param_change_time(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "config.py"
    _write(target, "TRADING_END = time(14, 45)\n")
    res = engineer._mut_param_change({
        "file": "helm/config.py",
        "symbol": "TRADING_END",
        "new_value": "14:30",
        "expected_type": "time",
    })
    assert res.ok, res.error
    assert "TRADING_END = time(14, 30)" in target.read_text()


def test_param_change_zero_matches(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "config.py"
    _write(target, "FOO = 42\n")
    res = engineer._mut_param_change({
        "file": "helm/config.py",
        "symbol": "ZOOM",
        "new_value": 1,
        "expected_type": "int",
    })
    assert not res.ok
    assert "0" in (res.error or "")


def test_param_change_two_matches(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "config.py"
    _write(target, "FOO = 42\nFOO = 43\n")
    res = engineer._mut_param_change({
        "file": "helm/config.py",
        "symbol": "FOO",
        "new_value": 1,
        "expected_type": "int",
    })
    assert not res.ok
    assert "2" in (res.error or "")


# ─── add_filter ───────────────────────────────────────────────────────

ORB_STUB = '''"""Stub ORB for tests."""

from helm.strategies.base import Signal, Strategy


class OpeningRangeBreakout(Strategy):
    def __init__(self, or_minutes: int = 15) -> None:
        self.or_minutes = or_minutes
        self._name = f"orb_{or_minutes}m"

    @property
    def name(self) -> str:
        return self._name

    def scan(self, symbol, candles):
        if len(candles) < self.or_minutes + 1:
            return None

        latest = candles[-1]
        return Signal(
            strategy=self.name,
            symbol=symbol,
            asof=latest["bar_ts"],
            side="BUY",
            entry_price=latest["close"],
            stop_loss=latest["low"],
            target=None,
            rationale="stub",
            payload={},
        )
'''


def test_add_filter_pre_insertion(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "strategies" / "intraday" / "orb.py"
    _write(target, ORB_STUB)
    res = engineer._mut_add_filter({
        "strategy": "orb_15m",
        "filter_id": "skip_choppy",
        "predicate_code": "len(candles) < 20",
        "where": "pre",
    })
    assert res.ok, res.error
    body = target.read_text()
    assert "# filter: skip_choppy" in body
    assert "if len(candles) < 20:" in body
    # Pre insertion lands after the early `return None` shape check.
    idx_shape = body.index("if len(candles) < self.or_minutes + 1:")
    idx_filter = body.index("if len(candles) < 20:")
    idx_signal = body.index("return Signal(")
    assert idx_shape < idx_filter < idx_signal


def test_add_filter_post_insertion(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "strategies" / "intraday" / "orb.py"
    _write(target, ORB_STUB)
    res = engineer._mut_add_filter({
        "strategy": "orb_15m",
        "filter_id": "min_or_width",
        "predicate_code": "True",
        "where": "post",
    })
    assert res.ok, res.error
    body = target.read_text()
    assert "# filter: min_or_width" in body
    idx_filter = body.index("if True:")
    idx_signal = body.index("return Signal(")
    assert idx_filter < idx_signal


def test_add_filter_dedup(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "strategies" / "intraday" / "orb.py"
    _write(target, ORB_STUB)
    spec = {
        "strategy": "orb_15m",
        "filter_id": "dup_marker",
        "predicate_code": "False",
        "where": "post",
    }
    first = engineer._mut_add_filter(spec)
    assert first.ok
    second = engineer._mut_add_filter(spec)
    assert not second.ok
    assert "marker" in second.summary or "marker" in (second.error or "")


# ─── add_strategy_variant ─────────────────────────────────────────────

STRATEGIES_INIT = '''"""Strategy registry."""
from helm.strategies.base import Strategy
from helm.strategies.intraday.orb import OpeningRangeBreakout

ACTIVE: list[Strategy] = [
    OpeningRangeBreakout(or_minutes=15),
]
'''


def test_add_strategy_variant(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "strategies" / "__init__.py"
    _write(target, STRATEGIES_INIT)
    res = engineer._mut_add_strategy_variant({
        "base_strategy": "OpeningRangeBreakout",
        "variant_name": "orb_3m",
        "param_overrides": {"or_minutes": 3},
    })
    assert res.ok, res.error
    body = target.read_text()
    assert "OpeningRangeBreakout(or_minutes=3)" in body
    assert "variant: orb_3m" in body


def test_add_strategy_variant_rejects_unknown_base(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "strategies" / "__init__.py"
    _write(target, STRATEGIES_INIT)
    res = engineer._mut_add_strategy_variant({
        "base_strategy": "NotAStrategy",
        "variant_name": "x",
        "param_overrides": {"a": 1},
    })
    assert not res.ok


# ─── bug_fix ──────────────────────────────────────────────────────────

def test_bug_fix_exact_anchor_replace(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "buggy.py"
    _write(target, "x = 1 + None  # bug\nprint(x)\n")
    res = engineer._mut_bug_fix({
        "target_file": "helm/buggy.py",
        "anchor": "x = 1 + None  # bug",
        "replacement": "x = 1 + 2  # fixed",
    })
    assert res.ok, res.error
    body = target.read_text()
    assert "x = 1 + 2  # fixed" in body
    assert "x = 1 + None" not in body


def test_bug_fix_rejects_ambiguous_anchor(tmp_repo: Path) -> None:
    target = tmp_repo / "helm" / "buggy.py"
    _write(target, "x = 0\nx = 0\n")
    res = engineer._mut_bug_fix({
        "target_file": "helm/buggy.py",
        "anchor": "x = 0",
        "replacement": "x = 1",
    })
    assert not res.ok


# ─── setting_override (stubbed DB) ────────────────────────────────────

def test_setting_override_upserts_via_set_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies _mut_setting_override delegates to store.set_setting with the
    'engineer-agent' actor and produces no file edits."""
    calls: list[tuple] = []

    def fake_set_setting(key, value, actor):
        calls.append((key, value, actor))

    monkeypatch.setattr("helm.data.store.set_setting", fake_set_setting)
    res = engineer._mut_setting_override({
        "key": "max_open_positions",
        "value": 3,
        "note": "tighten while learning",
    })
    assert res.ok
    assert res.files_touched == []
    assert res.pm2_reload_needed is False
    assert calls == [("max_open_positions", 3, "engineer-agent")]


# ─── _format_value direct unit tests ──────────────────────────────────

def test_format_value_int() -> None:
    assert engineer._format_value(99, "int") == "99"


def test_format_value_decimal() -> None:
    assert engineer._format_value("20000", "decimal") == 'Decimal("20000")'


def test_format_value_str() -> None:
    assert engineer._format_value("hi", "str") == '"hi"'


def test_format_value_time_list() -> None:
    assert engineer._format_value([14, 30], "time") == "time(14, 30)"


def test_format_value_time_hhmm() -> None:
    assert engineer._format_value("09:15", "time") == "time(9, 15)"


def test_format_value_bad_type() -> None:
    with pytest.raises(ValueError):
        engineer._format_value(1, "set")


# ─── process_one_task happy path (heavy stubbing) ─────────────────────

def test_process_one_task_happy_path(
    tmp_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full claim → mutate → gate → commit → release → done flow.

    All DB calls and git/subprocess work are stubbed; we verify the agent
    drives the lifecycle in the correct order with the right arguments.
    """
    # 1. Stub the base layer functions.
    claimed: dict = {
        "id": 17, "task_type": "prompt_tweak", "title": "tighten lunch-lull guidance",
        "spec": {
            "file": "scripts/decide_signals.py",
            "anchor": "Decision principles.",
            "action": "replace",
            "text": "Decision principles (revised).",
        },
    }
    completed: list[dict] = []
    released: list[dict] = []

    monkeypatch.setattr(engineer, "unverified_releases", lambda: [])
    monkeypatch.setattr(engineer, "claim_next_task",
                        lambda **kw: dict(claimed))

    def fake_complete(task_id, *, status, release_id=None, error=None):
        completed.append({"task_id": task_id, "status": status,
                          "release_id": release_id, "error": error})

    monkeypatch.setattr(engineer, "complete_task", fake_complete)

    def fake_record_release(*, task_id, commit_sha, summary, diff_stat=None,
                            branch="main"):
        released.append({"task_id": task_id, "commit_sha": commit_sha,
                         "summary": summary, "diff_stat": diff_stat})
        return 4242

    monkeypatch.setattr(engineer, "record_release", fake_record_release)

    runs: list[SimpleNamespace] = []

    @contextlib.contextmanager
    def fake_record_run(agent, invocation, **kw):
        handle = SimpleNamespace(
            run_id=1, agent=agent, started_at=0.0, trace={},
            outcome="ok", summary="", task_id=None, release_id=None,
            input_tokens=None, output_tokens=None,
            set=lambda **kw: handle.__dict__.update(kw),
            add_trace=lambda **kw: handle.trace.update(kw),
        )
        runs.append(handle)
        try:
            yield handle
        finally:
            pass

    monkeypatch.setattr(engineer, "record_run", fake_record_run)

    # 2. Stub git/subprocess: ruff and pytest both return 0; commit returns
    # a known sha; diff-stat is empty.
    def fake_run_cmd(args, *, cwd=None, timeout=120, check=True):
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(engineer, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(engineer, "git_commit_all",
                        lambda message: "deadbeefcafe1234")
    monkeypatch.setattr(engineer, "git_head_sha", lambda: "deadbeefcafe1234")
    monkeypatch.setattr(engineer, "git_diff_stat",
                        lambda rev="HEAD~1": " scripts/decide_signals.py | 2 +-")
    monkeypatch.setattr(engineer, "pm2_reload", lambda app="helm-dashboard": None)

    # 3. Lay the target prompt file on disk so the mutator has something to edit.
    target = tmp_repo / "scripts" / "decide_signals.py"
    _write(target, 'X = 1\n# anchor\n"""\nDecision principles.\n"""\n')

    # 4. Run.
    result = engineer.process_one_task()

    # 5. Verify lifecycle.
    assert result is not None
    assert result["ok"] is True
    assert result["task_id"] == 17
    assert result["release_id"] == 4242
    assert result["commit_sha"] == "deadbeefcafe1234"

    assert len(released) == 1
    assert released[0]["task_id"] == 17
    assert released[0]["commit_sha"] == "deadbeefcafe1234"

    # complete_task is called once, with status='done' and the release id.
    assert len(completed) == 1
    assert completed[0] == {"task_id": 17, "status": "done",
                            "release_id": 4242, "error": None}

    # run trace recorded the spec and the diff preview.
    assert len(runs) == 1
    rh = runs[0]
    assert rh.task_id == 17
    assert rh.release_id == 4242
    assert rh.outcome == "ok"
    assert "diff_preview" in rh.trace
    assert "Decision principles (revised)." in target.read_text()


def test_process_one_task_backpressure(monkeypatch: pytest.MonkeyPatch) -> None:
    """≥2 unverified releases → claim is skipped, returns None."""
    monkeypatch.setattr(engineer, "unverified_releases",
                        lambda: [{"id": 1}, {"id": 2}])

    def boom(**kw):
        raise AssertionError("claim_next_task should not be called under backpressure")

    monkeypatch.setattr(engineer, "claim_next_task", boom)

    assert engineer.process_one_task() is None


def test_process_one_task_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """No claimable task → None, no release row."""
    monkeypatch.setattr(engineer, "unverified_releases", lambda: [])
    monkeypatch.setattr(engineer, "claim_next_task", lambda **kw: None)

    @contextlib.contextmanager
    def fake_record_run(agent, invocation, **kw):
        handle = SimpleNamespace(
            run_id=1, trace={}, outcome="ok", summary="",
            task_id=None, release_id=None,
            set=lambda **kw: handle.__dict__.update(kw),
            add_trace=lambda **kw: handle.trace.update(kw),
        )
        yield handle

    monkeypatch.setattr(engineer, "record_run", fake_record_run)

    assert engineer.process_one_task() is None

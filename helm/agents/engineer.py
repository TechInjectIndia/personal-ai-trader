"""
Product-Engineer agent — applies one queued task per invocation.

The engineer is intentionally narrow: it dispatches on a closed set of
typed mutators (see ``ALLOWED_TYPES``). Each mutator takes the task ``spec``
dict, applies a deterministic change to disk (or the ``settings`` table),
and returns a :class:`MutatorResult`. The dispatcher in
:func:`process_one_task` wraps the work in ``record_run('engineer', ...)``
so every claim → mutate → commit → release is auditable.

Failure modes are explicit:

* Spec validation failure → task ``failed``, no rollback (no edits yet).
* Mutator failure (anchor not unique, duplicate marker, …) → task ``failed``.
* Lint / pytest failure after edit → ``git checkout -- .`` restores the tree,
  task ``failed``, no release row.
* Backpressure: if ≥2 unverified releases exist, we no-op and exit cleanly so
  we don't pile up commits while the tester is catching up.

The engineer NEVER pushes to a remote; ``git_commit_all`` is local only.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from helm.agents import base
from helm.agents.base import (
    RunHandle,
    claim_next_task,
    complete_task,
    git_commit_all,
    git_diff_stat,
    git_head_sha,
    pm2_reload,
    record_release,
    record_run,
    run_cmd,
    unverified_releases,
)

# Task types this agent can execute. Anything else stays as ``needs_human``.
ALLOWED_TYPES: set[str] = {
    "prompt_tweak", "param_change", "add_filter",
    "setting_override", "add_strategy_variant", "bug_fix",
}

# Files the prompt-tweak mutator is allowed to touch. ``bug_fix`` lifts this
# restriction (it can target any file) but still requires an exact-anchor
# unique replace.
PROMPT_FILES = {"scripts/decide_signals.py", "helm/retro.py"}

# Strategies the engineer knows how to build variants of. Hardcoded to keep
# spec validation honest — adding a new base class is a manual edit.
STRATEGY_CLASSES = {
    "OpeningRangeBreakout": "helm.strategies.intraday.orb",
    "VWAPReclaim": "helm.strategies.intraday.vwap",
    "GapFade": "helm.strategies.intraday.gap_fade",
}

# Maximum size of the diff preview we tuck into agent_runs.trace. 2 KB keeps
# the JSONB rows cheap to read in the dashboard.
TRACE_DIFF_LIMIT = 2048


# ─── result type ──────────────────────────────────────────────────────

@dataclass
class MutatorResult:
    ok: bool
    summary: str
    files_touched: list[str] = field(default_factory=list)
    error: str | None = None
    pm2_reload_needed: bool = False


# ─── helpers ──────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _resolve_repo_path(rel: str) -> Path:
    """Resolve a spec-supplied relative path against the active repo root.

    We read ``base.REPO_ROOT`` dynamically (instead of importing the constant
    at module-load time) so tests can monkey-patch the repo root to a tmp dir.
    """
    root = base.REPO_ROOT
    p = (root / rel).resolve()
    # Guard against ``..`` shenanigans pointing outside the repo.
    try:
        p.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path {rel!r} escapes repo root") from exc
    return p


def _count_anchor(text: str, anchor: str) -> int:
    if not anchor:
        return 0
    return text.count(anchor)


def _diff_preview(before: str, after: str, *, limit: int = TRACE_DIFF_LIMIT) -> str:
    """Cheap unified-ish diff: just truncated before/after for trace.

    We don't shell out to ``diff`` because the tests run on stubbed git state;
    a coarse before/after slice is fine for the audit trail.
    """
    import difflib
    lines = list(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        n=2, lineterm="",
    ))
    out = "".join(lines)
    if len(out) > limit:
        return out[:limit] + "\n…[truncated]"
    return out


def _format_value(value: object, expected_type: str) -> str:
    """Render the RHS literal for a param_change replacement."""
    if expected_type == "int":
        return str(int(value))  # type: ignore[arg-type]
    if expected_type == "decimal":
        return f'Decimal("{value}")'
    if expected_type == "str":
        # Re-encode through json to escape embedded quotes/specials.
        return json.dumps(str(value))
    if expected_type == "time":
        # Spec passes either [h, m] or "HH:MM" — accept both.
        if isinstance(value, (list, tuple)) and len(value) == 2:
            h, m = int(value[0]), int(value[1])
        elif isinstance(value, str) and ":" in value:
            hs, ms = value.split(":", 1)
            h, m = int(hs), int(ms)
        else:
            raise ValueError(f"time value {value!r} must be [h, m] or 'HH:MM'")
        return f"time({h}, {m})"
    raise ValueError(f"unknown expected_type {expected_type!r}")


# ─── mutators ─────────────────────────────────────────────────────────

def _mut_prompt_tweak(spec: dict) -> MutatorResult:
    """Append / replace / prepend a paragraph keyed off a unique anchor."""
    try:
        rel = spec["file"]
        anchor = spec["anchor"]
        action = spec["action"]
        text = spec["text"]
    except KeyError as exc:
        return MutatorResult(False, "missing spec field", error=f"missing {exc}")

    if action not in {"append", "replace", "prepend"}:
        return MutatorResult(False, "bad action",
                             error=f"action must be append/replace/prepend, got {action!r}")
    if rel not in PROMPT_FILES:
        return MutatorResult(False, "file not in prompt set",
                             error=f"{rel} not in {sorted(PROMPT_FILES)}")

    path = _resolve_repo_path(rel)
    if not path.exists():
        return MutatorResult(False, "file missing", error=f"{rel} not found")

    before = _read(path)
    count = _count_anchor(before, anchor)
    if count != 1:
        return MutatorResult(False, f"anchor matched {count}× (need 1)",
                             error=f"anchor {anchor!r} found {count} times in {rel}")

    after = _apply_anchor_edit(before, anchor, action, text)
    if after == before:
        return MutatorResult(False, "no-op edit", error="edit produced no change")
    _write(path, after)
    return MutatorResult(
        ok=True,
        summary=f"{action} near anchor in {rel}",
        files_touched=[rel],
        pm2_reload_needed=False,
    )


def _apply_anchor_edit(text: str, anchor: str, action: str, payload: str) -> str:
    """The text-transform half of prompt_tweak, exposed for unit-testing."""
    idx = text.find(anchor)
    if idx < 0:
        return text
    if action == "replace":
        return text[:idx] + payload + text[idx + len(anchor):]
    # For append/prepend we work in terms of the anchor's paragraph (the run
    # of non-blank lines containing the anchor). We find the bounds and stitch.
    line_start = text.rfind("\n", 0, idx) + 1
    line_end = text.find("\n", idx)
    if line_end < 0:
        line_end = len(text)
    if action == "append":
        # Walk forward until the next blank line (or EOF).
        cursor = line_end
        while cursor < len(text):
            nxt = text.find("\n", cursor + 1)
            if nxt < 0:
                cursor = len(text)
                break
            # A blank line is two consecutive newlines.
            if text[cursor:cursor + 2] == "\n\n":
                break
            cursor = nxt
        return text[:cursor] + "\n\n" + payload + text[cursor:]
    # prepend: walk backward to the previous blank line (or BOF).
    cursor = line_start
    while cursor > 0:
        prev = text.rfind("\n", 0, cursor - 1)
        if prev < 0:
            cursor = 0
            break
        if text[prev:prev + 2] == "\n\n":
            cursor = prev + 1
            break
        cursor = prev + 1
    return text[:cursor] + payload + "\n\n" + text[cursor:]


def _mut_param_change(spec: dict) -> MutatorResult:
    """Rewrite a top-level constant assignment.

    Matches ``^SYMBOL\\s*[:=]\\s*<rhs>`` once at module level; refuses on 0 or
    2+ matches. Type-formats the new RHS via :func:`_format_value`.
    """
    try:
        rel = spec["file"]
        symbol = spec["symbol"]
        new_value = spec["new_value"]
        expected_type = spec["expected_type"]
    except KeyError as exc:
        return MutatorResult(False, "missing spec field", error=f"missing {exc}")

    path = _resolve_repo_path(rel)
    if not path.exists():
        return MutatorResult(False, "file missing", error=f"{rel} not found")

    try:
        rendered = _format_value(new_value, expected_type)
    except (ValueError, TypeError) as exc:
        return MutatorResult(False, "bad value", error=str(exc))

    before = _read(path)
    # Module-level: line must start with the symbol (no indentation). The RHS
    # is everything up to the first '#' (comment) or newline so we preserve
    # the inline comment if any. We accept two forms:
    #   SYMBOL = value                  (plain assignment)
    #   SYMBOL: T = value               (PEP 526 annotated assignment)
    # The capture is (prefix, rhs, comment-suffix).
    pattern = re.compile(
        rf"^({re.escape(symbol)}(?:\s*:\s*[^=\n#]+?)?\s*=\s*)"
        r"([^#\n]+?)(\s*(?:#[^\n]*)?)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(before))
    if len(matches) != 1:
        return MutatorResult(False, f"symbol matched {len(matches)}× (need 1)",
                             error=f"symbol {symbol!r} found {len(matches)} times in {rel}")
    m = matches[0]
    after = before[:m.start()] + m.group(1) + rendered + m.group(3) + before[m.end():]
    if after == before:
        return MutatorResult(False, "no-op edit", error="edit produced no change")
    _write(path, after)
    # config.py touches mean PM2 should reload so the dashboard re-imports.
    pm2 = rel in {"helm/config.py"}
    return MutatorResult(
        ok=True,
        summary=f"{symbol} → {rendered} in {rel}",
        files_touched=[rel],
        pm2_reload_needed=pm2,
    )


def _mut_add_filter(spec: dict) -> MutatorResult:
    """Insert an ``if predicate: return None`` guard in a strategy's scan()."""
    try:
        strategy = spec["strategy"]
        filter_id = spec["filter_id"]
        predicate = spec["predicate_code"]
        where = spec["where"]
    except KeyError as exc:
        return MutatorResult(False, "missing spec field", error=f"missing {exc}")
    if where not in {"pre", "post"}:
        return MutatorResult(False, "bad where",
                             error=f"where must be pre/post, got {where!r}")

    target = _find_strategy_file(strategy)
    if target is None:
        return MutatorResult(False, "strategy not found",
                             error=f"no strategy with name={strategy!r}")
    rel = target.relative_to(base.REPO_ROOT).as_posix()
    before = _read(target)

    marker = f"# filter: {filter_id}"
    if marker in before:
        return MutatorResult(False, "duplicate marker",
                             error=f"marker {marker!r} already in {rel}")

    after = _insert_filter(before, predicate, filter_id, where)
    if after is None:
        return MutatorResult(False, "scan() not found",
                             error=f"could not locate def scan(...) in {rel}")
    if after == before:
        return MutatorResult(False, "no-op edit", error="edit produced no change")
    _write(target, after)
    return MutatorResult(
        ok=True,
        summary=f"add_filter {filter_id} ({where}) to {strategy}",
        files_touched=[rel],
        pm2_reload_needed=False,
    )


def _find_strategy_file(strategy_name: str) -> Path | None:
    """Find the .py file under helm/strategies/ whose ``name`` matches.

    Matches ``name = "X"`` (class attribute) or ``self._name = f"X"`` /
    ``self._name = "X"`` patterns. Returns the resolved path or None.
    """
    strategies_dir = base.REPO_ROOT / "helm" / "strategies"
    if not strategies_dir.exists():
        return None
    for py in strategies_dir.rglob("*.py"):
        if py.name in {"__init__.py", "base.py"}:
            continue
        text = py.read_text(encoding="utf-8")
        # Direct: name = "vwap_reclaim"
        if re.search(rf'^\s*name\s*=\s*[\'"]{re.escape(strategy_name)}[\'"]',
                     text, re.MULTILINE):
            return py
        # Parameterised: ORB sets self._name = f"orb_{or_minutes}m"
        # We match the prefix before the first '{' or end of the literal.
        for m in re.finditer(r'self\._name\s*=\s*f?[\'"]([^\'"{}]*)', text):
            prefix = m.group(1)
            if strategy_name == prefix or strategy_name.startswith(prefix):
                return py
    return None


def _insert_filter(text: str, predicate: str, filter_id: str, where: str) -> str | None:
    """Insert the guarded if-block in ``def scan(self, ...)``.

    ``pre``  → immediately after the first ``return None`` early-exit shape
               check inside scan().
    ``post`` → immediately before the final ``return Signal(`` call.
    """
    scan_re = re.compile(r"^(\s*)def\s+scan\s*\(", re.MULTILINE)
    scan_match = scan_re.search(text)
    if not scan_match:
        return None
    indent = scan_match.group(1) + "    "
    block = (
        f"{indent}if {predicate}:\n"
        f"{indent}    return None  # filter: {filter_id}\n"
    )

    # Slice from end of `def scan(...)` onwards.
    scan_body_start = text.find("\n", scan_match.end()) + 1
    rest = text[scan_body_start:]

    if where == "pre":
        # Find the first `return None` line inside scan().
        m = re.search(r"^(\s*)return None\s*$", rest, re.MULTILINE)
        if not m:
            return None
        insert_at = scan_body_start + m.end()
        # Ensure a leading newline before block.
        return text[:insert_at] + "\n" + block + text[insert_at:]
    # post: find the last `return Signal(` inside scan().
    last_return = None
    for m in re.finditer(r"^\s*return Signal\(", rest, re.MULTILINE):
        last_return = m
    if last_return is None:
        return None
    insert_at = scan_body_start + last_return.start()
    return text[:insert_at] + block + text[insert_at:]


def _mut_setting_override(spec: dict) -> MutatorResult:
    """UPSERT a row in the ``settings`` table. No file change."""
    try:
        key = spec["key"]
        value = spec["value"]
        note = spec.get("note", "")
    except KeyError as exc:
        return MutatorResult(False, "missing spec field", error=f"missing {exc}")
    if not isinstance(key, str) or not key:
        return MutatorResult(False, "bad key", error="key must be non-empty str")

    # Imported lazily so unit-tests that monkey-patch out the DB layer don't
    # blow up at module load.
    from helm.data.store import set_setting
    set_setting(key, value, actor="engineer-agent")
    summary = f"setting {key}={value!r}"
    if note:
        summary += f" — {note}"
    return MutatorResult(
        ok=True,
        summary=summary,
        files_touched=[],
        pm2_reload_needed=False,
    )


def _mut_add_strategy_variant(spec: dict) -> MutatorResult:
    """Append a new instance of an existing strategy class to ACTIVE."""
    try:
        base_strategy = spec["base_strategy"]
        variant_name = spec["variant_name"]
        overrides = spec["param_overrides"]
    except KeyError as exc:
        return MutatorResult(False, "missing spec field", error=f"missing {exc}")

    if base_strategy not in STRATEGY_CLASSES:
        return MutatorResult(
            False, "unknown base strategy",
            error=f"{base_strategy} not in {sorted(STRATEGY_CLASSES)}",
        )
    if not isinstance(overrides, dict):
        return MutatorResult(False, "bad overrides",
                             error="param_overrides must be a dict")

    rel = "helm/strategies/__init__.py"
    path = _resolve_repo_path(rel)
    before = _read(path)

    # Render kwargs deterministically (sorted) so repeat runs are stable.
    kwargs = ", ".join(f"{k}={_repr_literal(v)}" for k, v in sorted(overrides.items()))
    new_line = f"    {base_strategy}({kwargs}),  # variant: {variant_name}\n"
    if new_line in before:
        return MutatorResult(False, "variant already present",
                             error=f"variant {variant_name!r} line already in {rel}")

    # The ACTIVE list literal ends with `]`. Insert before it.
    close_re = re.compile(r"(ACTIVE\s*:\s*list\[Strategy\]\s*=\s*\[[\s\S]*?)(\n\])",
                          re.MULTILINE)
    m = close_re.search(before)
    if not m:
        return MutatorResult(False, "ACTIVE list not found",
                             error="couldn't locate ACTIVE list in helm/strategies/__init__.py")
    insert_at = m.start(2)  # right before the closing "\n]"
    after = before[:insert_at] + "\n" + new_line.rstrip("\n") + before[insert_at:]
    _write(path, after)
    return MutatorResult(
        ok=True,
        summary=f"add variant {variant_name} of {base_strategy}",
        files_touched=[rel],
        pm2_reload_needed=True,
    )


def _repr_literal(v: object) -> str:
    """Cheap python-literal renderer for variant kwargs."""
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    raise ValueError(f"unsupported override value type: {type(v).__name__}")


def _mut_bug_fix(spec: dict) -> MutatorResult:
    """Single-shot exact-anchor replace, broader file scope than prompt_tweak."""
    try:
        target_file = spec.get("target_file") or spec["file"]
        anchor = spec["anchor"]
        replacement = spec.get("replacement", spec.get("text"))
    except KeyError as exc:
        return MutatorResult(False, "missing spec field", error=f"missing {exc}")
    if replacement is None:
        return MutatorResult(False, "missing replacement",
                             error="bug_fix needs 'replacement' (or 'text')")

    path = _resolve_repo_path(target_file)
    if not path.exists():
        return MutatorResult(False, "file missing", error=f"{target_file} not found")

    before = _read(path)
    count = _count_anchor(before, anchor)
    if count != 1:
        return MutatorResult(False, f"anchor matched {count}× (need 1)",
                             error=f"anchor {anchor!r} found {count} times in {target_file}")
    after = before.replace(anchor, replacement, 1)
    if after == before:
        return MutatorResult(False, "no-op edit", error="replacement equals anchor")
    _write(path, after)
    return MutatorResult(
        ok=True,
        summary=f"bug_fix replace in {target_file}",
        files_touched=[target_file],
        pm2_reload_needed=False,
    )


# Registry of dispatchable mutators. Keyed by task_type.
MUTATORS: dict[str, Callable[[dict], MutatorResult]] = {
    "prompt_tweak":        _mut_prompt_tweak,
    "param_change":        _mut_param_change,
    "add_filter":          _mut_add_filter,
    "setting_override":    _mut_setting_override,
    "add_strategy_variant": _mut_add_strategy_variant,
    "bug_fix":             _mut_bug_fix,
}


def apply_mutator(task_type: str, spec: dict) -> MutatorResult:
    """Dispatch to the registered mutator; safe on unknown types."""
    fn = MUTATORS.get(task_type)
    if fn is None:
        return MutatorResult(False, f"no mutator for {task_type!r}",
                             error=f"unsupported task_type {task_type}")
    return fn(spec)


# ─── gates ────────────────────────────────────────────────────────────

def _run_ruff() -> subprocess.CompletedProcess:
    return run_cmd(["ruff", "check", "helm", "scripts"], check=False)


def _run_pytest() -> subprocess.CompletedProcess:
    return run_cmd(["pytest", "-q", "tests/"], check=False, timeout=300)


def _restore_tree(files: list[str] | None = None) -> None:
    """Undo the current task's edits — narrowly.

    A blanket ``git checkout -- .`` would also clobber any unrelated
    tracked-but-modified files the human is mid-editing, which silently
    discards their in-progress work. We only revert the files the mutator
    actually touched; if none were tracked yet (e.g. setting_override didn't
    modify any source), this is a no-op.
    """
    if not files:
        return
    # `git checkout --` errors on paths that aren't tracked. Run per-file
    # with check=False so we tolerate that and don't blow up the run.
    for rel in files:
        run_cmd(["git", "checkout", "--", rel], check=False)


# ─── main loop ────────────────────────────────────────────────────────

def process_one_task() -> dict | None:
    """Claim, mutate, gate, commit, release. Returns a summary dict or None.

    None means: nothing claimed (either no open tasks for us, or backpressure
    from too many unverified releases). Otherwise the caller gets a dict with
    enough context to print a one-liner.
    """
    # Backpressure check BEFORE we claim — saves a needless claim/unclaim if
    # the tester is behind.
    pending = unverified_releases()
    if len(pending) >= 2:
        return None

    with record_run("engineer", "process_one_task") as run:
        return _process_one_task_inner(run)


def _process_one_task_inner(run: RunHandle) -> dict | None:
    task = claim_next_task(claimed_by="engineer-agent", allow_types=ALLOWED_TYPES)
    if not task:
        run.outcome = "noop"
        run.summary = "no tasks to claim"
        return None

    task_id = task["id"]
    task_type = task["task_type"]
    title = task["title"]
    spec = task["spec"] or {}
    if isinstance(spec, str):
        # JSONB usually returns dict via dict_row, but be defensive.
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError:
            spec = {}

    run.set(task_id=task_id)
    run.add_trace(task_type=task_type, title=title, spec=spec)

    if task_type not in MUTATORS:
        msg = f"unsupported task_type {task_type!r}"
        complete_task(task_id, status="failed", error=msg)
        run.outcome = "error"
        run.summary = msg
        return {"ok": False, "task_id": task_id, "reason": msg}

    # Snapshot tree state so the diff-preview reflects the actual edit.
    touched_before: dict[str, str] = {}
    for rel in _likely_touched_files(task_type, spec):
        try:
            p = _resolve_repo_path(rel)
            if p.exists():
                touched_before[rel] = p.read_text(encoding="utf-8")
        except Exception:
            pass

    result = apply_mutator(task_type, spec)
    if not result.ok:
        complete_task(task_id, status="failed", error=result.error or result.summary)
        run.outcome = "error"
        run.summary = f"mutator failed: {result.error or result.summary}"
        return {"ok": False, "task_id": task_id, "reason": result.error}

    # Compute a small diff preview to stash in the trace.
    diff_blob = ""
    for rel in result.files_touched:
        try:
            p = _resolve_repo_path(rel)
            after = p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            after = ""
        before = touched_before.get(rel, "")
        diff_blob += _diff_preview(before, after, limit=TRACE_DIFF_LIMIT)
        if len(diff_blob) >= TRACE_DIFF_LIMIT:
            break
    if len(diff_blob) > TRACE_DIFF_LIMIT:
        diff_blob = diff_blob[:TRACE_DIFF_LIMIT] + "\n…[truncated]"
    if diff_blob:
        run.add_trace(diff_preview=diff_blob)

    # Lint gate. Skipped when no file change (setting_override): nothing to lint.
    if result.files_touched:
        ruff = _run_ruff()
        if ruff.returncode != 0:
            _restore_tree(result.files_touched)
            err = (ruff.stdout or "") + (ruff.stderr or "")
            complete_task(task_id, status="failed", error=f"ruff: {err[:1500]}")
            run.outcome = "error"
            run.summary = "ruff failed"
            run.add_trace(ruff_stderr=err[:2000])
            return {"ok": False, "task_id": task_id, "reason": "ruff failed"}

        # Pytest gate.
        pyt = _run_pytest()
        if pyt.returncode != 0:
            _restore_tree(result.files_touched)
            err = (pyt.stdout or "") + (pyt.stderr or "")
            complete_task(task_id, status="failed", error=f"pytest: {err[:1500]}")
            run.outcome = "error"
            run.summary = "pytest failed"
            run.add_trace(pytest_stderr=err[:2000])
            return {"ok": False, "task_id": task_id, "reason": "pytest failed"}

    # Commit (unless purely a settings change with no file touches).
    commit_sha: str | None = None
    if result.files_touched:
        message = _commit_message(task_id, title, spec, result.files_touched)
        try:
            commit_sha = git_commit_all(message)
        except subprocess.CalledProcessError as exc:
            _restore_tree(result.files_touched)
            err = (exc.stderr or exc.stdout or str(exc))[:1500]
            complete_task(task_id, status="failed", error=f"git commit: {err}")
            run.outcome = "error"
            run.summary = "git commit failed"
            run.add_trace(commit_stderr=err)
            return {"ok": False, "task_id": task_id, "reason": "git commit failed"}
    else:
        # No file change AND no settings change → nothing to release.
        if task_type != "setting_override":
            complete_task(task_id, status="failed",
                          error="mutator produced no commit and no settings change")
            run.outcome = "error"
            run.summary = "empty mutator"
            return {"ok": False, "task_id": task_id, "reason": "empty mutator"}
        # setting_override: no commit, but still record a release row off HEAD.
        try:
            commit_sha = git_head_sha()
        except subprocess.CalledProcessError:
            commit_sha = "0000000000"

    diff_stat = ""
    if result.files_touched:
        try:
            diff_stat = git_diff_stat("HEAD~1")
        except Exception:
            diff_stat = ""

    release_id = record_release(
        task_id=task_id,
        commit_sha=commit_sha or "0000000000",
        summary=result.summary,
        diff_stat=diff_stat or None,
    )
    run.set(release_id=release_id)

    complete_task(task_id, status="done", release_id=release_id)

    if result.pm2_reload_needed:
        pm2_reload()

    run.outcome = "ok"
    run.summary = f"[task {task_id}] {result.summary}"
    return {
        "ok": True,
        "task_id": task_id,
        "release_id": release_id,
        "commit_sha": commit_sha,
        "summary": result.summary,
        "files_touched": result.files_touched,
    }


def _commit_message(task_id: int, title: str, spec: dict, files: list[str]) -> str:
    """Standard commit message format. No co-author trailer (bot commits)."""
    spec_json = json.dumps(spec, indent=2, default=str, sort_keys=True)
    files_csv = ", ".join(files) if files else "(no files)"
    return (
        f"[task #{task_id}] {title}\n"
        f"\n"
        f"spec:\n{spec_json}\n"
        f"\n"
        f"files: {files_csv}\n"
    )


def _likely_touched_files(task_type: str, spec: dict) -> list[str]:
    """Best-effort prediction of which files a mutator will touch.

    Used only to grab a "before" snapshot for the trace diff. Wrong answers
    here are harmless — the post-edit phase reads result.files_touched.
    """
    if task_type in {"prompt_tweak", "bug_fix"}:
        f = spec.get("file") or spec.get("target_file")
        return [f] if isinstance(f, str) else []
    if task_type == "param_change":
        f = spec.get("file")
        return [f] if isinstance(f, str) else []
    if task_type == "add_strategy_variant":
        return ["helm/strategies/__init__.py"]
    if task_type == "add_filter":
        return []  # determined dynamically; cheap-but-empty preview is ok
    return []

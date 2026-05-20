"""LLM adapter — black-box decider engine.

Two interchangeable backends:

  api  — anthropic SDK direct call with prompt caching (production target).
         Costs API tokens; honors prompt caching for the system message.

  cli  — shell out to a headless coding-CLI (POC cost-saver). The default
         CLI backend is `claude -p`, which uses the user's Claude Code
         subscription auth (Max plan), so token usage counts against
         subscription rate limits rather than billed API spend. See
         WORKAROUNDS.md for tradeoffs and the post-POC removal path.

Same input → same output regardless of mode. Callers do not branch on backend.
Mode is selected by env var LLM_MODE (default `cli` while in POC).

CLI backend registry
--------------------
The `cli` mode dispatches to one of several headless coding CLIs so the
competition league can run each agent on a different vendor's CLI:

  claude · gemini · qwen · codex · opencode

The backend is chosen (in order of precedence) by the explicit `backend`
argument, then the `LLM_BACKEND` env var, then the default `claude`. Each
backend has a small adapter that builds the correct headless command and
parses stdout to a JSON dict (tolerantly, via `_parse_json`). The `claude`
adapter is the proven path and is preserved byte-for-byte; the other
adapters concatenate system+user into one prompt (those CLIs have no
separate system-prompt flag) and tolerate prose around the JSON.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

LLM_MODE_DEFAULT = "cli"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_BACKEND = "claude"
# 300s is enough headroom for the PM agent's heavy weekly-review prompt
# (24+ open proposals + retros + engineer-run history can run 60-180s on
# Sonnet 4.6). Decider calls return in 5-10s so they never approach this.
CLI_TIMEOUT_S = 300

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["TAKE", "SKIP"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning"],
}

CLI_RULES_SUFFIX = """

--- HEADLESS / CLI ORCHESTRATION RULES ---
You are running through the Claude Code CLI in non-interactive print mode.
Disregard any default tendency to behave as a coding assistant. Specifically:
  * Do NOT call tools, read files, search the codebase, or browse the web.
  * Do NOT ask clarifying questions — there is no human to answer them.
  * Do NOT add preamble, commentary, markdown fences, or trailing prose.
Output ONLY the JSON object that satisfies the schema above. The caller
fail-closes (defaults to SKIP) on any unparseable byte.
"""

# Generic variant of CLI_RULES_SUFFIX for non-claude CLIs, which have no
# separate system-prompt flag and no built-in JSON-schema enforcement.
GENERIC_CLI_RULES_SUFFIX = """

--- HEADLESS / CLI ORCHESTRATION RULES ---
You are running through a coding CLI in non-interactive mode.
Disregard any default tendency to behave as a coding assistant. Specifically:
  * Do NOT call tools, read files, search the codebase, or browse the web.
  * Do NOT ask clarifying questions — there is no human to answer them.
  * Do NOT add preamble, commentary, markdown fences, or trailing prose.
Output ONLY a single JSON object that satisfies the requested schema. The
caller fail-closes (defaults to SKIP) on any unparseable byte.
"""


class LLMError(RuntimeError):
    """Raised when the backend transport fails or returns unparseable output."""


def decide(system: str, user: str, *, model: str | None = None,
           mode: str | None = None, backend: str | None = None,
           max_tokens: int = 1024, temperature: float = 0.0) -> dict:
    """Ask the LLM for a TAKE/SKIP verdict.

    Returns a dict {verdict: "TAKE"|"SKIP", confidence: float, reasoning: str}.
    Raises LLMError on transport failure or unparseable output.

    `backend` selects the CLI vendor (claude/gemini/qwen/codex/opencode) when
    in cli mode; it defaults to the proven claude path so existing callers are
    unaffected.
    """
    raw = complete_json(
        system, user,
        schema=VERDICT_SCHEMA,
        model=model, mode=mode, backend=backend,
        max_tokens=max_tokens, temperature=temperature,
    )
    return _normalize_verdict(raw)


def complete_json(system: str, user: str, *, schema: dict,
                  model: str | None = None, mode: str | None = None,
                  backend: str | None = None,
                  max_tokens: int = 1024, temperature: float = 0.0) -> dict:
    """Generic schema-typed completion. Returns the raw parsed JSON object.

    Same api/cli backend selection and same prompt-cache pattern as decide().
    Callers normalise/validate the returned dict themselves.

    In cli mode, `backend` (or the LLM_BACKEND env var) chooses which coding
    CLI to shell out to; it defaults to `claude`, so when no backend is given
    and LLM_MODE is unset/`cli` the behaviour is identical to the original
    single-backend implementation.
    """
    model = model or os.environ.get("DECIDER_MODEL", DEFAULT_MODEL)
    mode = (mode or os.environ.get("LLM_MODE", LLM_MODE_DEFAULT)).strip().lower()

    if mode == "api":
        return _complete_api(system, user, model=model,
                             max_tokens=max_tokens, temperature=temperature)
    if mode == "cli":
        return _complete_cli(system, user, model=model, schema=schema,
                             backend=backend)
    raise LLMError(f"unknown LLM_MODE={mode!r}; expected 'api' or 'cli'")


def _complete_api(system: str, user: str, *, model: str,
                  max_tokens: int, temperature: float) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("LLM_MODE=api but ANTHROPIC_API_KEY not set")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _parse_json(text)


def _complete_cli(system: str, user: str, *, model: str, schema: dict,
                  backend: str | None = None,
                  timeout_s: int = CLI_TIMEOUT_S) -> dict:
    """Dispatch a cli-mode completion to the selected backend adapter.

    Backend precedence: explicit `backend` arg > LLM_BACKEND env > claude.
    The default (claude) is the proven path and is unchanged.
    """
    name = (backend or os.environ.get("LLM_BACKEND", DEFAULT_BACKEND)).strip().lower()
    adapter = CLI_ADAPTERS.get(name)
    if adapter is None:
        raise LLMError(
            f"unknown LLM_BACKEND={name!r}; expected one of "
            f"{', '.join(sorted(CLI_ADAPTERS))}"
        )
    return adapter(system, user, model=model, schema=schema, timeout_s=timeout_s)


def _resolve_cli(name: str, env_var: str) -> str:
    """Locate a CLI binary on PATH (or via an override env var)."""
    cli = shutil.which(name) or os.environ.get(env_var)
    if not cli:
        raise LLMError(f"`{name}` CLI not on PATH and {env_var} unset")
    return cli


def _run_cli(cmd: list[str], *, name: str, timeout_s: int) -> str:
    """Run a CLI command headlessly and return stdout, raising LLMError on failure."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd="/tmp",
        )
    except FileNotFoundError as exc:
        raise LLMError(f"{name} CLI not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"{name} CLI timeout after {timeout_s}s") from exc

    if proc.returncode != 0:
        # Some CLIs echo the whole prompt and run-metadata before the actual
        # error (e.g. codex prints its config banner + the echoed prompt, then
        # an auth 401 at the very end). The real failure is usually in the
        # tail, so keep both head and tail; callers such as
        # scripts/smoke_backends.py classify on this error text.
        raise LLMError(
            f"{name} CLI exit {proc.returncode}: "
            f"{_head_tail(proc.stderr or proc.stdout)}"
        )
    return proc.stdout


def _head_tail(text: str, head: int = 600, tail: int = 600) -> str:
    """Keep the first `head` and last `tail` chars of long CLI output so both
    the run banner and the trailing error survive truncation."""
    t = text or ""
    if len(t) <= head + tail:
        return t
    return f"{t[:head]} …[{len(t) - head - tail} chars elided]… {t[-tail:]}"


def _adapter_claude(system: str, user: str, *, model: str, schema: dict,
                    timeout_s: int) -> dict:
    """Proven claude-cli adapter. Preserved byte-for-byte from the original
    `_complete_cli` implementation — do not change its command shape."""
    cli = shutil.which("claude") or os.environ.get("CLAUDE_CLI_PATH")
    if not cli:
        raise LLMError("`claude` CLI not on PATH and CLAUDE_CLI_PATH unset")

    injected_system = system + CLI_RULES_SUFFIX
    # --tools "" already removes all tools so no permission prompts can occur.
    # --dangerously-skip-permissions belt-and-suspenders: ensures the harness
    # never blocks on a permission decision in headless mode.
    cmd = [
        cli, "-p", user,
        "--system-prompt", injected_system,
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        "--json-schema", json.dumps(schema),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd="/tmp",
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude CLI timeout after {timeout_s}s") from exc

    if proc.returncode != 0:
        raise LLMError(
            f"claude CLI exit {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}"
        )

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"claude CLI non-JSON stdout: {proc.stdout[:300]!r}") from exc

    if envelope.get("is_error"):
        raise LLMError(f"claude CLI envelope error: {envelope.get('result', '')[:300]}")

    # With --json-schema, the schema-conformant object is delivered separately
    # in `structured_output`; `result` typically holds chattier prose.
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return _parse_json(envelope.get("result", ""))


def _build_generic_prompt(system: str, user: str) -> str:
    """Concatenate system + user for CLIs that have no separate system flag."""
    return f"{system}{GENERIC_CLI_RULES_SUFFIX}\n\n=== TASK ===\n{user}"


def _adapter_gemini(system: str, user: str, *, model: str, schema: dict,
                    timeout_s: int) -> dict:
    """Google Gemini CLI (`gemini -p`). Google login. No system-prompt flag,
    so system+user are concatenated and stdout is parsed tolerantly."""
    cli = _resolve_cli("gemini", "GEMINI_CLI_PATH")
    prompt = _build_generic_prompt(system, user)
    cmd = [cli, "-p", prompt]
    stdout = _run_cli(cmd, name="gemini", timeout_s=timeout_s)
    return _parse_json(stdout)


def _adapter_qwen(system: str, user: str, *, model: str, schema: dict,
                  timeout_s: int) -> dict:
    """Qwen Code CLI (`qwen -p`). No system-prompt flag; tolerant parse."""
    cli = _resolve_cli("qwen", "QWEN_CLI_PATH")
    prompt = _build_generic_prompt(system, user)
    cmd = [cli, "-p", prompt]
    stdout = _run_cli(cmd, name="qwen", timeout_s=timeout_s)
    return _parse_json(stdout)


def _adapter_codex(system: str, user: str, *, model: str, schema: dict,
                   timeout_s: int) -> dict:
    """OpenAI Codex CLI (`codex exec`). ChatGPT login. Tolerant parse over the
    exec transcript (Codex prints run metadata around the final answer).

    `--skip-git-repo-check` lets it run outside a trusted git repo (we exec
    from /tmp); `--sandbox read-only` keeps it from attempting writes."""
    cli = _resolve_cli("codex", "CODEX_CLI_PATH")
    prompt = _build_generic_prompt(system, user)
    cmd = [
        cli, "exec",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        prompt,
    ]
    stdout = _run_cli(cmd, name="codex", timeout_s=timeout_s)
    return _parse_json(stdout)


def _adapter_opencode(system: str, user: str, *, model: str, schema: dict,
                      timeout_s: int) -> dict:
    """opencode CLI (`opencode run`). Provider login via its own config.
    Tolerant parse."""
    cli = _resolve_cli("opencode", "OPENCODE_CLI_PATH")
    prompt = _build_generic_prompt(system, user)
    cmd = [cli, "run", prompt]
    stdout = _run_cli(cmd, name="opencode", timeout_s=timeout_s)
    return _parse_json(stdout)


# Backend registry: name -> adapter. Adapters share a uniform signature
# (system, user, *, model, schema, timeout_s) -> dict. `claude` is the default
# and the proven path; the others are best-effort headless wrappers.
AdapterFn = Callable[..., dict]
CLI_ADAPTERS: dict[str, AdapterFn] = {
    "claude": _adapter_claude,
    "gemini": _adapter_gemini,
    "qwen": _adapter_qwen,
    "codex": _adapter_codex,
    "opencode": _adapter_opencode,
}


def _parse_json(text: str) -> dict:
    """Tolerant JSON object extraction."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1:
        raise LLMError(f"no JSON object in model output: {text[:200]!r}")
    try:
        return json.loads(t[s:e + 1])
    except json.JSONDecodeError as exc:
        # Free/varied CLIs sometimes emit braces around non-JSON prose. Surface
        # this as LLMError so callers' `except LLMError` fail-closed paths (the
        # decider's SKIP, the competition runner's skip+fallback) handle it
        # instead of an uncaught JSONDecodeError crashing the cycle.
        raise LLMError(f"malformed JSON in model output: {text[:200]!r}") from exc


def _normalize_verdict(parsed: dict) -> dict:
    verdict = str(parsed.get("verdict", "SKIP")).upper()
    if verdict not in ("TAKE", "SKIP"):
        verdict = "SKIP"
    return {
        "verdict": verdict,
        "confidence": float(parsed.get("confidence", 0.0)),
        "reasoning": str(parsed.get("reasoning", ""))[:1000],
    }

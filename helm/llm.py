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
The `cli` mode dispatches to one of several backends so the competition league
can run each agent on a different vendor's model:

  claude · gemini · qwen · nemotron · opencode

`claude`, `gemini`, and `opencode` shell out to headless coding CLIs; `qwen`
(qwen3-next) and `nemotron` call OpenRouter's HTTP API directly (free models,
model id per competitor). The backend is chosen (in order of precedence) by the
explicit `backend` argument, then the `LLM_BACKEND` env var, then the default
`claude`. Each backend has a small adapter. The `claude` adapter is the proven
path and is preserved byte-for-byte; the CLI adapters concatenate system+user
into one prompt (those CLIs have no separate system-prompt flag) and tolerate
prose around the JSON; the OpenRouter adapter sends a real system/user message
pair and parses the response content tolerantly.
"""

from __future__ import annotations

import json
import os
import re
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
    """Google Gemini CLI (`gemini -p`). Auth via GEMINI_API_KEY (or Google
    login). No system-prompt flag, so system+user are concatenated and stdout
    is parsed tolerantly.

    `--skip-trust` bypasses the CLI's workspace-trust gate: run unattended from
    the repo, gemini otherwise exits 55 ("not running in a trusted directory")."""
    cli = _resolve_cli("gemini", "GEMINI_CLI_PATH")
    prompt = _build_generic_prompt(system, user)
    cmd = [cli, "--skip-trust", "-p", prompt]
    stdout = _run_cli(cmd, name="gemini", timeout_s=timeout_s)
    return _parse_json(stdout)


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Free OpenRouter models leave reasoning/output headroom; $0 spend so be generous.
OPENROUTER_MAX_TOKENS = 2048


def _adapter_openrouter(system: str, user: str, *, model: str, schema: dict,
                        timeout_s: int) -> dict:
    """OpenRouter HTTP adapter — used by the `qwen` and `nemotron` backends.

    Unlike the subprocess CLIs, this calls OpenRouter's OpenAI-compatible chat
    completions API directly over HTTPS, so `model` is REQUIRED (it is the
    OpenRouter model id, e.g. `qwen/qwen3-next-80b-a3b-instruct:free` or
    `nvidia/nemotron-3-super-120b-a12b:free`) and comes from the competitor's
    `model` column. Auth via OPENROUTER_API_KEY in the environment (.env).

    OpenRouter free models share an account-wide daily request cap; when it is
    exhausted the API returns HTTP 429, whose body trips the competition quota
    subsystem's rate-limit detector (`quota.note_error`) so the backend
    auto-pauses and resumes next window. Output is parsed tolerantly — the
    caller fail-closes on any unparseable byte.

    `response_format={"type":"json_object"}` is passed so providers that support
    it (most chat-completions backends do) constrain the model to emit valid
    JSON instead of chain-of-thought prose; providers that don't ignore it."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise LLMError("OPENROUTER_API_KEY not set (required for OpenRouter backends)")
    if not model:
        raise LLMError("OpenRouter backend requires an explicit model id")
    import requests

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": OPENROUTER_MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system + GENERIC_CLI_RULES_SUFFIX},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Optional attribution headers OpenRouter recommends for app traffic.
        "HTTP-Referer": "https://helm.techinject.co.in",
        "X-Title": "helm-competition-league",
    }
    try:
        resp = requests.post(OPENROUTER_URL, json=payload, headers=headers,
                             timeout=timeout_s)
    except requests.Timeout as exc:
        raise LLMError(f"openrouter timeout after {timeout_s}s") from exc
    except requests.RequestException as exc:
        raise LLMError(f"openrouter transport error: {exc}") from exc

    if resp.status_code != 200:
        # Keep the body (truncated) so quota.note_error can spot 429/rate-limit.
        raise LLMError(f"openrouter HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"openrouter malformed response: {resp.text[:300]!r}") from exc
    return _parse_json(text)


OPENCODE_DEFAULT_MODEL = "opencode/big-pickle"


def _adapter_opencode(system: str, user: str, *, model: str, schema: dict,
                      timeout_s: int) -> dict:
    """opencode CLI (`opencode run`), pinned to its bundled gateway model.

    We MUST pass `-m provider/model`: opencode auto-detects provider keys from
    the environment, so once the runner loads .env (which carries GEMINI_API_KEY
    for the gemini backend) an unpinned `opencode run` silently switches provider
    and returns empty output. Pinning the model forces the free gateway and
    ignores stray keys. Only an opencode-style `provider/model` id is valid for
    -m; a non-matching id (e.g. the claude default passed by the smoke test) is
    ignored in favour of the bundled gateway. Tolerant parse on stdout."""
    cli = _resolve_cli("opencode", "OPENCODE_CLI_PATH")
    prompt = _build_generic_prompt(system, user)
    oc_model = model if (model and "/" in model) else OPENCODE_DEFAULT_MODEL
    cmd = [cli, "run", "-m", oc_model, prompt]
    stdout = _run_cli(cmd, name="opencode", timeout_s=timeout_s)
    return _parse_json(stdout)


KIRO_DEFAULT_CMD = "kiro-cli"


def _adapter_kiro(system: str, user: str, *, model: str, schema: dict,
                  timeout_s: int) -> dict:
    """Kiro CLI adapter — AWS Kiro CLI (`kiro-cli chat`), free tier.

    CLI CONTRACT
    ------------
    Binary: ``kiro-cli`` (override via ``KIRO_CLI_CMD``). Invocation::

        kiro-cli chat --no-interactive --trust-tools= --wrap never "<prompt>"

    ``chat`` takes the prompt as a single positional argument, so system + user
    are merged via ``_build_generic_prompt`` (chat has no separate system flag).
    ``--no-interactive`` runs headless; ``--trust-tools=`` forbids ALL tool use
    (a trading decider must never run shell/fs tools); ``--wrap never`` keeps the
    output unwrapped. The model is left at Kiro's default ("auto") — the
    per-competitor ``model`` value is intentionally ignored, since the free plan
    selects the model itself.

    OUTPUT
    ------
    Kiro draws its TUI chrome (logo, banners, tips, the ``--trust-tools`` warning
    that literally contains ``@{MCPSERVERNAME}``, and a credits footer) to
    **stderr**, which ``_run_cli`` discards. **stdout** carries only ANSI colour
    codes, a ``> `` prompt echo, and the model's answer, so we strip ANSI then
    extract the JSON tolerantly with ``_parse_json``.

    Auth: ``kiro-cli`` logs in via Builder ID (state under ``~/.kiro``); see
    ``docs/kiro-agent-setup.md``. Raises ``LLMError`` on non-zero exit or
    unparseable output.
    """
    kiro_cmd = os.environ.get("KIRO_CLI_CMD", KIRO_DEFAULT_CMD)
    cli = shutil.which(kiro_cmd)
    if cli is None and os.path.isabs(kiro_cmd) and os.path.exists(kiro_cmd):
        cli = kiro_cmd
    if not cli:
        raise LLMError(
            f"`{kiro_cmd}` CLI not on PATH and KIRO_CLI_CMD does not point to a "
            "valid binary — install/login the Kiro CLI or set KIRO_CLI_CMD"
        )
    prompt = _build_generic_prompt(system, user)
    cmd = [cli, "chat", "--no-interactive", "--trust-tools=", "--wrap", "never", prompt]
    stdout = _run_cli(cmd, name="kiro", timeout_s=timeout_s)
    return _parse_json(_strip_ansi(stdout))


# Backend registry: name -> adapter. Adapters share a uniform signature
# (system, user, *, model, schema, timeout_s) -> dict. `claude` is the default
# and the proven path; `gemini`/`opencode` are headless CLI wrappers; `qwen` and
# `nemotron` run via OpenRouter's HTTP API (model id supplied per-competitor);
# `kiro` shells out to the Kiro CLI (see _adapter_kiro docstring + docs/kiro-agent-setup.md).
AdapterFn = Callable[..., dict]
CLI_ADAPTERS: dict[str, AdapterFn] = {
    "claude": _adapter_claude,
    "gemini": _adapter_gemini,
    "kiro": _adapter_kiro,
    "qwen": _adapter_openrouter,       # qwen/qwen3-next-80b-a3b-instruct:free via OpenRouter
    "nemotron": _adapter_openrouter,   # nvidia/nemotron-3-super-120b-a12b:free via OpenRouter
    "opencode": _adapter_opencode,
}


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI/VT100 escape sequences (some CLIs colourize stdout, e.g. Kiro)."""
    return _ANSI_RE.sub("", text or "")


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

"""LLM adapter — black-box decider engine.

Two interchangeable backends:

  api  — anthropic SDK direct call with prompt caching (production target).
         Costs API tokens; honors prompt caching for the system message.

  cli  — shell out to `claude -p` (POC cost-saver). Uses the user's Claude
         Code subscription auth (Max plan), so token usage counts against
         subscription rate limits rather than billed API spend. See
         WORKAROUNDS.md for tradeoffs and the post-POC removal path.

Same input → same output regardless of mode. Callers do not branch on backend.
Mode is selected by env var LLM_MODE (default `cli` while in POC).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

LLM_MODE_DEFAULT = "cli"
DEFAULT_MODEL = "claude-sonnet-4-6"
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


class LLMError(RuntimeError):
    """Raised when the backend transport fails or returns unparseable output."""


def decide(system: str, user: str, *, model: str | None = None,
           mode: str | None = None, max_tokens: int = 1024,
           temperature: float = 0.0) -> dict:
    """Ask the LLM for a TAKE/SKIP verdict.

    Returns a dict {verdict: "TAKE"|"SKIP", confidence: float, reasoning: str}.
    Raises LLMError on transport failure or unparseable output.
    """
    raw = complete_json(
        system, user,
        schema=VERDICT_SCHEMA,
        model=model, mode=mode,
        max_tokens=max_tokens, temperature=temperature,
    )
    return _normalize_verdict(raw)


def complete_json(system: str, user: str, *, schema: dict,
                  model: str | None = None, mode: str | None = None,
                  max_tokens: int = 1024, temperature: float = 0.0) -> dict:
    """Generic schema-typed completion. Returns the raw parsed JSON object.

    Same api/cli backend selection and same prompt-cache pattern as decide().
    Callers normalise/validate the returned dict themselves.
    """
    model = model or os.environ.get("DECIDER_MODEL", DEFAULT_MODEL)
    mode = (mode or os.environ.get("LLM_MODE", LLM_MODE_DEFAULT)).strip().lower()

    if mode == "api":
        return _complete_api(system, user, model=model,
                             max_tokens=max_tokens, temperature=temperature)
    if mode == "cli":
        return _complete_cli(system, user, model=model, schema=schema)
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


def _complete_cli(system: str, user: str, *, model: str, schema: dict) -> dict:
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
            timeout=CLI_TIMEOUT_S,
            cwd="/tmp",
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude CLI timeout after {CLI_TIMEOUT_S}s") from exc

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
    return json.loads(t[s:e + 1])


def _normalize_verdict(parsed: dict) -> dict:
    verdict = str(parsed.get("verdict", "SKIP")).upper()
    if verdict not in ("TAKE", "SKIP"):
        verdict = "SKIP"
    return {
        "verdict": verdict,
        "confidence": float(parsed.get("confidence", 0.0)),
        "reasoning": str(parsed.get("reasoning", ""))[:1000],
    }

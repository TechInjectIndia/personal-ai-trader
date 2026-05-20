"""Smoke-test the competition CLI backends in helm.llm's registry.

Runs ONE tiny headless call per backend (claude, gemini, qwen, codex,
opencode) asking for a trivial JSON object, classifies each backend, and
prints a result matrix. Designed to be re-run by the human after they log
the NEEDS-AUTH backends in.

Classifications:
  OK            valid JSON returned by the backend
  NEEDS-AUTH    CLI runs but demands an interactive login / no creds
  NOT-INSTALLED CLI binary not on PATH
  BAD-OUTPUT    CLI ran but produced no parseable JSON / other error

This script makes NO API calls and writes NO secrets. The claude backend
uses the Claude Code subscription; the others use whatever auth the human
has configured for those CLIs. $0 spend.

Usage:
  python scripts/smoke_backends.py
  python scripts/smoke_backends.py --backend gemini   # one backend only
  python scripts/smoke_backends.py --timeout 20

Exit code is always 0 (it's a diagnostic, not a gate).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time

from helm import llm

# Fast smoke timeout — never use the 300s production timeout for diagnostics.
SMOKE_TIMEOUT_S = 30

# Tiny system + user prompt that any model can satisfy with a one-line object.
SMOKE_SYSTEM = (
    "You are a JSON output service. Reply with exactly one JSON object and "
    "nothing else."
)
SMOKE_USER = 'Return this exact JSON object and nothing else: {"ok": true}'
SMOKE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}

# Map each backend to the binary name + the env override consulted by its
# adapter, so we can report "NOT-INSTALLED" without invoking the CLI.
_BINARIES: dict[str, str] = {
    "claude": "claude",
    "gemini": "gemini",
    "qwen": "qwen",
    "codex": "codex",
    "opencode": "opencode",
}

# Substrings that, when present in an error, indicate the CLI ran but wants
# an interactive login rather than a transport/output failure.
_AUTH_HINTS = (
    "login",
    "log in",
    "logged in",
    "authenticate",
    "authentication",
    "auth",
    "unauthorized",
    "unauthenticated",
    "not signed in",
    "sign in",
    "credentials",
    "api key",
    "api_key",
    "no auth",
    "oauth",
    "permission",
    "403",
    "401",
)


def _classify(backend: str, timeout_s: int) -> tuple[str, str]:
    """Run one smoke call for `backend`. Returns (classification, note)."""
    if shutil.which(_BINARIES[backend]) is None:
        return "NOT-INSTALLED", f"`{_BINARIES[backend]}` not on PATH"

    adapter = llm.CLI_ADAPTERS[backend]
    started = time.monotonic()
    try:
        result = adapter(
            SMOKE_SYSTEM, SMOKE_USER,
            model=llm.DEFAULT_MODEL,
            schema=SMOKE_SCHEMA,
            timeout_s=timeout_s,
        )
    except llm.LLMError as exc:
        msg = str(exc)
        elapsed = time.monotonic() - started
        low = msg.lower()
        if any(h in low for h in _AUTH_HINTS):
            return "NEEDS-AUTH", _trim(msg)
        if "timeout" in low:
            return "BAD-OUTPUT", f"timed out after {elapsed:.0f}s: {_trim(msg)}"
        return "BAD-OUTPUT", _trim(msg)
    except Exception as exc:  # noqa: BLE001 — diagnostic must never crash the matrix
        return "BAD-OUTPUT", f"{type(exc).__name__}: {_trim(str(exc))}"

    elapsed = time.monotonic() - started
    if isinstance(result, dict):
        return "OK", f"{result!r} in {elapsed:.1f}s"
    return "BAD-OUTPUT", f"non-dict result: {result!r}"


def _trim(text: str, n: int = 160) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= n else one_line[: n - 1] + "…"


def _print_matrix(rows: list[tuple[str, str, str]]) -> None:
    headers = ("backend", "classification", "notes")
    widths = [
        max(len(headers[0]), *(len(r[0]) for r in rows)),
        max(len(headers[1]), *(len(r[1]) for r in rows)),
        len(headers[2]),
    ]
    line = f"{headers[0]:<{widths[0]}}  {headers[1]:<{widths[1]}}  {headers[2]}"
    print(line)
    print("-" * (widths[0] + widths[1] + len(headers[2]) + 4))
    for backend, klass, note in rows:
        print(f"{backend:<{widths[0]}}  {klass:<{widths[1]}}  {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test LLM CLI backends.")
    parser.add_argument(
        "--backend",
        choices=sorted(llm.CLI_ADAPTERS),
        help="Test only this backend (default: all).",
    )
    parser.add_argument(
        "--timeout", type=int, default=SMOKE_TIMEOUT_S,
        help=f"Per-call timeout in seconds (default {SMOKE_TIMEOUT_S}).",
    )
    args = parser.parse_args(argv)

    backends = [args.backend] if args.backend else sorted(llm.CLI_ADAPTERS)
    print(
        f"Smoke-testing {len(backends)} backend(s) with a "
        f"{args.timeout}s timeout each...\n"
    )

    rows: list[tuple[str, str, str]] = []
    for backend in backends:
        klass, note = _classify(backend, args.timeout)
        rows.append((backend, klass, note))
        print(f"  {backend:<10} -> {klass}")

    print()
    _print_matrix(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

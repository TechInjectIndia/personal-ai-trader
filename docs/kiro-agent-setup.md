# Kiro Agent Setup Guide

This document explains how to install, authenticate, and activate the **kiro**
competitor in the helm competition league.  The agent is pre-registered in the
database as `kiro-orb` (status = `paused`) so the live loop cannot invoke it
until the steps below are complete.

---

## 1. Install the Kiro CLI

Kiro is Amazon's AI coding assistant.  Its headless CLI is distributed as an
npm package:

```bash
# Install globally (requires Node.js >= 18)
npm install -g @amazon/kiro-cli

# Verify the binary is on PATH
kiro --version
```

If `npm install` places the binary somewhere not on PATH (common with npm
global installs under nvm or system Node), add the npm global bin directory:

```bash
# Find the location
npm bin -g

# Then add to your shell profile, e.g.:
export PATH="$(npm bin -g):$PATH"
```

Alternatively, override the binary path via the environment variable
`KIRO_CLI_CMD` (no PATH change needed):

```bash
export KIRO_CLI_CMD="/path/to/kiro"
```

---

## 2. Authenticate the Kiro CLI

Kiro uses AWS credentials or a dedicated Kiro API key depending on your
subscription:

**Option A — AWS Builder ID (personal, free tier):**
```bash
kiro auth login --provider builderid
# Follow the browser OAuth flow.
```

**Option B — KIRO_API_KEY (team/enterprise):**
```bash
export KIRO_API_KEY="<your-key>"
# Add to .env so helm scripts pick it up automatically.
```

After authentication, verify Kiro can run headlessly:

```bash
kiro run --no-interactive --json \
  --prompt "Reply with exactly this JSON object and nothing else: {\"ok\": true}"
# Expected stdout: {"ok": true}
```

---

## 3. CLI command contract expected by `_adapter_kiro`

`helm/llm.py:_adapter_kiro` invokes Kiro as follows:

```
kiro run \
  --system  /tmp/helm_kiro_system.txt \
  --prompt  /tmp/helm_kiro_user.txt   \
  --json                               \
  --no-interactive
```

| Detail | Value |
|--------|-------|
| Binary | `kiro` (or `$KIRO_CLI_CMD`) |
| System prompt file | `/tmp/helm_kiro_system.txt` (written on every call, includes `GENERIC_CLI_RULES_SUFFIX`) |
| User prompt file | `/tmp/helm_kiro_user.txt` (written on every call) |
| Required flags | `--json` (bare JSON stdout), `--no-interactive` (suppress prompts) |
| Expected stdout | A single JSON object matching the requested schema, e.g. `{"verdict":"SKIP","confidence":0.82,"reasoning":"..."}` |
| Exit code on success | `0` |
| Exit code on failure | Non-zero (adapter raises `LLMError`) |

The adapter uses `_parse_json` (tolerant brace-extraction) so minor leading
prose or ``` fences in stdout are stripped automatically.

Environment override:

| Variable | Purpose |
|----------|---------|
| `KIRO_CLI_CMD` | Override binary path/name (default: `kiro`) |
| `KIRO_API_KEY` | API key auth for enterprise Kiro installs |

---

## 4. Smoke-test the backend

`scripts/smoke_backends.py` tests all registered backends with a trivial one-line
JSON call and classifies each result:

```bash
# Test kiro only (fast, 30s timeout)
source .venv/bin/activate
python scripts/smoke_backends.py --backend kiro

# Expected output when working:
#   kiro       -> OK
#   kiro  OK  {'ok': True} in 4.2s

# Test all backends at once (shows the full competition matrix):
python scripts/smoke_backends.py
```

Classification meanings:

| Label | Meaning |
|-------|---------|
| `OK` | Kiro returned valid JSON — ready to trade |
| `NOT-INSTALLED` | `kiro` binary not on PATH; follow step 1 |
| `NEEDS-AUTH` | Binary ran but wants a login; follow step 2 |
| `BAD-OUTPUT` | Binary ran but output was not parseable JSON; check `--json` flag support |

---

## 5. Un-pause kiro once smoke test passes

The `kiro-orb` competitor is seeded with `status='paused'`.
`helm/competition/competitors.py:freestyle_competitors()` filters on
`status = 'active'`, so `scripts/run_competitors.py` will not invoke kiro
until you flip the status.

Run this SQL against the `helm` database (as the OS user that owns the DB):

```sql
UPDATE competitors
   SET status = 'active'
 WHERE id = 'kiro-orb';
```

Or using `psql`:

```bash
psql dbname=helm -c "UPDATE competitors SET status = 'active' WHERE id = 'kiro-orb';"
```

Verify:

```bash
psql dbname=helm -c "SELECT id, status FROM competitors WHERE id = 'kiro-orb';"
# Expected: kiro-orb | active
```

Then do a manual dry-run to confirm end-to-end:

```bash
source .venv/bin/activate
python scripts/run_competitors.py --competitor kiro-orb --dry-run --force-window
```

If the output shows `[kiro] ok:` with a decision — no `FAIL` or `PAUSED` — the
agent is live-ready.  Remove `--dry-run` and `--force-window` from the crontab
entry (or let the existing cron pick it up on the next 5-min tick).

---

## 6. Kiro persona — Opening-Range Breakout (ORB)

**Competitor ID:** `kiro-orb`  
**Name:** Kiro (Opening-Range Breakout)  
**Backend:** `kiro`  
**Niche:** Gap-and-go / ORB

The kiro agent's strategy persona is distinct from the other five league members:

| Competitor | Niche |
|-----------|-------|
| house-claude | House incumbent (multi-strategy decider) |
| gemini-momentum | Momentum breakout chaser |
| qwen-meanrev | Mean-reversion / VWAP-reclaim |
| nemotron-trend | Disciplined trend-follower |
| opencode-range | Range trader / ETF scalper |
| **kiro-orb** | **Opening-range breakout / gap-and-go** |

The kiro-orb agent waits for the first 15-minute candle to define the day's
opening range, then enters on a volume-confirmed break of that range's high
(long) or low (short).  Hard stop at the opposite extreme; target is 2× the
range width.  It holds cash and ignores all intra-range action.

---

## 7. Quota and rate-limit behaviour

`helm/config.py` sets kiro's backend quota conservatively at **200 calls per
24-hour window** (matching the gemini tier) until Kiro's actual daily limits
are confirmed.  If kiro returns a rate-limit error the quota subsystem
auto-pauses the backend until the window rolls.

To adjust after real limits are known, edit `BACKEND_QUOTAS["kiro"]` in
`helm/config.py` and restart PM2:

```bash
pm2 restart helm-dashboard
```

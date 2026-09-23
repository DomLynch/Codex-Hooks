#!/usr/bin/env python3
"""PreToolUse: the one-deployer rule, made mechanical. Ported from the Claude install on 2026-09-23.

While Frankendom's deploy.sh runs it holds a lock file. A Mac shared by a dozen lanes cannot run their browser
checks, test suites and bakes at the same time without the deploy taking three times as long (measured
2026-09-21: two 100 s checks took 15 min each under load 30+). So while a fresh lock exists, this hook refuses
the heavy commands from every other session with a one-line reason and the time already spent, and lets
everything else through. A lock whose pid is dead or older than STALE_S is ignored: a killed deploy cannot wedge
anyone.

TWO THINGS DIFFER FROM THE CLAUDE COPY, both deliberate:

1. LOCK_PATH still defaults to ~/.claude/state/deploy_in_flight.json, NOT a ~/.codex path. The rule is one
   deployer on this Mac, not one deployer per agent; pointing Codex at its own lock file would let a Codex
   session run a full browser suite straight through a Claude lane's deploy and recreate the exact contention
   this exists to stop. Both agents read the same lock. Override with DEPLOY_LOCK if that ever stops being true.

2. Codex's shell tool is `exec`, not `Bash`, and its command does not arrive as a bare string — the observed
   shape is a JS wrapper, `tools.exec_command({cmd: "..."})`. Rather than depend on a payload shape that is not
   documented and may change, the heavy-command match runs over the WHOLE serialized tool_input. A quoted
   mention of a heavy command that is never executed would therefore also be deferred; during an active deploy
   that is the behaviour we want anyway, and outside one there is no lock so nothing is ever refused.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Shared with the Claude install on purpose — see (1) in the module docstring.
LOCK_PATH = Path(os.environ.get("DEPLOY_LOCK") or Path.home() / ".claude" / "state" / "deploy_in_flight.json")
STALE_S = 45 * 60

# Codex names its shell tool `exec`; `Bash` is kept so a shared payload or a future rename still matches.
SHELL_TOOLS = {"exec", "Bash", "shell", "exec_command"}

# What competes with a deploy for the Mac: real browsers, whole test suites, builds and bakes. Unit tests of one
# file, git, gh, curl, greps and edits are not on this list on purpose.
HEAVY_RE = re.compile(
    r"(?:^|[\s;&|(\\\"'])(?:"
    r"npx\s+playwright\b|node\s+scripts/[\w-]*(?:check|preview|gate)[\w-]*\.mjs|node\s+artifacts/\S+browser\S*\.mjs"
    r"|npm\s+run\s+(?:test:all|test:slow|quality(?::\w+)?|build|release-checks?)\b|node\s+scripts/release-checks\.mjs"
    r"|node\s+--test\s+tests/\*|node\s+--test\s*$|node\s+--test\s+[^\s]+\s+[^\s]+"
    r"|(?:\S*/)?[Bb]lender\b|vite\s+build\b|meshopt\S*|gltfpack\b"
    r")"
)
ALLOW_RE = re.compile(r"scripts/deploy\.sh\b")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _is_descendant_of(pid: int, ancestor: int) -> bool:
    """Is this hook (and so the tool call) running under the deployer's process tree?"""
    import subprocess
    seen = 0
    while pid > 1 and seen < 64:
        if pid == ancestor:
            return True
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
            pid = int(out) if out else 0
        except Exception:
            return False
        seen += 1
    return False


def active_lock(now: datetime | None = None) -> dict | None:
    try:
        data = json.loads(LOCK_PATH.read_text())
        pid = int(data.get("pid", 0))
        started = datetime.fromisoformat(str(data.get("started", "")).replace("Z", "+00:00"))
    except Exception:
        return None
    now = now or datetime.now(timezone.utc)
    age = (now - started).total_seconds()
    if age < 0 or age > STALE_S or not _pid_alive(pid):
        return None
    data["age_s"] = int(age)
    return data


def command_text(tool_input: object) -> str:
    """Everything in the tool input that could carry a shell command, as one string — see (2) in the docstring."""
    if isinstance(tool_input, str):
        return tool_input
    try:
        return json.dumps(tool_input)
    except Exception:
        return str(tool_input)


def decide(command: str, lock: dict | None, own_pid: int | None = None) -> str:
    """Return the deny reason, or '' to allow."""
    if not lock or not command or ALLOW_RE.search(command) or not HEAVY_RE.search(command):
        return ""
    if own_pid is not None and _is_descendant_of(own_pid, int(lock.get("pid", 0))):
        return ""
    mins = lock["age_s"] // 60
    return (
        f"Deploy {str(lock.get('revision', ''))[:7]} is in flight "
        f"(started {str(lock.get('started', '?'))[11:16]}Z, {mins} min ago) — "
        "one-deployer rule: browser checks, test suites, builds and bakes wait until the deploy dev posts FREE. "
        "Deploys take 5-10 min on a free Mac; retry then. Light commands (git, gh, grep, single-file tests) are "
        "not blocked."
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    tool = str(payload.get("tool_name") or "")
    if tool.rsplit("__", 1)[-1] not in SHELL_TOOLS:
        return
    reason = decide(command_text(payload.get("tool_input")), active_lock(), own_pid=os.getpid())
    if not reason:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))


if __name__ == "__main__":
    main()

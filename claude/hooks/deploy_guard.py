#!/usr/bin/env python3
"""PreToolUse (Bash): the one-deployer rule, made mechanical.

While Frankendom's deploy.sh runs it holds ~/.claude/state/deploy_in_flight.json. A Mac shared by a dozen lanes
cannot run their browser checks, test suites and bakes at the same time without the deploy taking three times as
long (measured 2026-09-21: two 100 s checks took 15 min each under load 30+). So while a fresh lock exists, this
hook refuses the heavy commands from every other session with a one-line reason and the time already spent, and
lets everything else through. The deployer's own session (the lock's pid is its child) is never blocked, and neither
is deploy.sh itself. A lock whose pid is dead or older than STALE_S is ignored: a killed deploy cannot wedge anyone.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCK_PATH = Path(os.environ.get("DEPLOY_LOCK") or Path.home() / ".claude" / "state" / "deploy_in_flight.json")
STALE_S = 45 * 60
# What competes with a deploy for the Mac: real browsers, whole test suites, builds and bakes. Unit tests of one file,
# git, gh, curl, greps and edits are not on this list on purpose.
HEAVY_RE = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"npx\s+playwright\b|node\s+scripts/[\w-]*(?:check|preview|gate|quality|battery)[\w-]*\.mjs|node\s+artifacts/\S+browser\S*\.mjs"
    r"|npm\s+run\s+(?:test:all|test:slow|test:browser|quality(?::\w+)?|build|release-checks?)\b|npm\s+(?:run\s+)?test\b(?!:)"
    r"|node\s+scripts/release-checks\.mjs"
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
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
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


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
SUBSTITUTION_RE = re.compile(r"(?<!\\)\$\(([^)]*)\)|(?<!\\)`((?:[^`\\]|\\.)*)`")   # \` and \$( are literal text
SHELL = r"(?:^|[\s;&|(/])(?:ba|z)?sh"   # a shell command, not a file named x.sh
RUNS_ITS_ARGUMENT_RE = re.compile(rf"(?:{SHELL}\s+-\w*c|(?:^|[\s;&|(])eval)\s*$")   # bash -c "...", zsh -lc '...', eval '...'
SHELL_STDIN_RE = re.compile(rf"{SHELL}\s*$")                                         # bash <<EOF: the body is run


def executable_text(command: str) -> str:
    """The parts of a shell line that run: commit messages, PR bodies and script heredocs are data, not commands."""
    def heredoc(m: re.Match) -> str:
        return m.group(0) if SHELL_STDIN_RE.search(command[:m.start()].rstrip()) else ""

    def quoted(m: re.Match) -> str:
        inner = m.group(0)[1:-1]
        if RUNS_ITS_ARGUMENT_RE.search(stripped[:m.start()]):
            return f" {inner} "
        # "$(npm test)" and "`npm test`" run even inside double quotes; single quotes run nothing.
        if m.group(0)[0] == "'":
            return '""'
        return '"" ' + " ; ".join(a or b for a, b in SUBSTITUTION_RE.findall(inner))

    stripped = HEREDOC_RE.sub(heredoc, command)
    return QUOTED_RE.sub(quoted, stripped)


def decide(command: str, lock: dict | None, own_pid: int | None = None) -> str:
    """Return the deny reason, or '' to allow."""
    runs = executable_text(command) if lock and command else ""
    if not runs or ALLOW_RE.search(runs) or not HEAVY_RE.search(runs):
        return ""
    if own_pid is not None and _is_descendant_of(own_pid, int(lock.get("pid", 0))):
        return ""
    mins = lock["age_s"] // 60
    return (
        f"Deploy {str(lock.get('revision', ''))[:7]} is in flight (started {lock.get('started', '?')[11:16]}Z, {mins} min ago) — "
        "one-deployer rule: browser checks, test suites, builds and bakes wait until the deploy dev posts FREE. "
        "Deploys take 5–10 min on a free Mac; retry then. Light commands (git, gh, grep, single-file tests) are not blocked."
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    if payload.get("tool_name") != "Bash":
        return
    command = str((payload.get("tool_input") or {}).get("command") or "")
    reason = decide(command, active_lock(), own_pid=os.getpid())
    if not reason:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason},
    }))


if __name__ == "__main__":
    main()

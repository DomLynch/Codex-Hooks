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
# What competes with a deploy for the Mac: real browsers, whole test suites, builds and bakes.
# 2026-09-26: herolook-game-stills / herolook-stills (Playwright + Vite), herolook-kill-record (the sim) and build-creatures
# (spawns blender) ran through the a981f7a5 lock: the name list needs stills, record and build-.
# armour-contact-sheet (Playwright) and any herolook-*.mjs render are heavy too; the python herolook_*.py helpers are light and
# never match here (python, .py). Unit tests of one file,
# git, gh, curl, greps and edits are not on this list on purpose.
HEAVY_RE = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"npx\s+playwright\b|node\s+scripts/[\w-]*(?:check|preview|gate|quality|battery|stills|record|build-|contact-sheet|herolook-|pixelmator-|textures-)[\w-]*\.mjs|node\s+artifacts/\S+browser\S*\.mjs"
    r"|npm\s+run\s+(?:test:all|test:slow|test:browser|quality(?::\w+)?|build|release-checks?)\b|npm\s+(?:run\s+)?test\b(?!:)"
    r"|node\s+scripts/release-checks\.mjs"
    r"|node\s+--test\s+tests/\*|node\s+--test\s*$|node\s+--test(?:\s+-[^\s]*)*\s+[^-\s][^\s]*\s+[^-\s][^\s]*"
    r"|(?:\S*/)?[Bb]lender\b|vite\s+build\b|meshopt\S*|gltfpack\b"
    r")"
)
# An allowed segment RUNS the deploy script: optional VAR=x / env / sh|bash|zsh prefix, then the path as the command word.
# Merely naming it (test -f scripts/deploy.sh, cat, echo) exempts nothing, and each ; && || | ( ) segment is judged alone.
ALLOW_RE = re.compile(r"^\s*(?:(?:env\s+|\w+=\S*\s+|(?:ba|z)?sh\s+(?:-\w+\s+)*))*(?:\S*/)?scripts/deploy\.sh\b")
SEGMENT_SPLIT_RE = re.compile(r"[;&|()`\n]+")
# Pixelmator Pro (Hero Look's headless texture step, 2026-09-26: ML super resolution + export via osascript) is heavy. The app is
# named inside the osascript argument's quotes, which executable_text() strips as data, so this looks at the RAW command: a real
# osascript / open -a invocation (in the executable text) whose own segment (no ; && || between) names Pixelmator, inline -e,
# a .scpt/.applescript path, a heredoc or `open -a`. Plain osascript (notifications) and a commit message naming Pixelmator pass.
APP_HOST_RE = re.compile(r"(?:^|[\s;&|(])(?:osascript\b|open\s+-[ab]\b)")
# Same line and segment (a newline or single | ends the command: 2026-09-27 review found `osascript -e notify` then
# `git commit -m pixelmator` on the next line was blocked), or inside the osascript heredoc's own body.
PIXELMATOR_RE = re.compile(
    r"(?:osascript\b|open\s+-[ab]\b)[^\n;|&]*?pixelmator"
    r"|osascript\b[^\n;|&]*<<-?\s*['\"]?(\w+)\b['\"]?[^\n]*\n(?:(?!\n\s*\1\s*(?:\n|$)).)*?pixelmator",
    re.IGNORECASE | re.DOTALL)


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


# 2026-10-08 (Dom, Optimizer item 2): heavy runs are refused on the Mac at any time, not only under the deploy lock (Mac load sat at
# 29-57 on 10 cores from lane test/browser runs). Exempt: the deploy itself (ALLOW_RE / lock holder), Safari/WebKit and real-iPhone rows,
# the sparring GPU row, and MAC_HEAVY_OK=1 for when the VPS is down.
_ROW = r"(?:webkit|safari|iphone|simctl|sparring)"
# Judged per heavy segment: the exempt word must be the command or script (npm script name, scripts/ file, --project), never a path,
# branch or test-file argument; MAC_HEAVY_OK=1 only as that segment's own leading env (Auditor on #104).
MAC_EXEMPT_RE = re.compile(
    rf"^\s*(?:\w+=\S*\s+)*MAC_HEAVY_OK=1\b|^\s*(?:\w+=\S*\s+)*(?:xcrun\s+simctl\b|npm\s+run\s+[\w:-]*{_ROW}"
    rf"|node\s+(?:\S*/)?scripts/[\w-]*{_ROW}|npx\s+playwright\s+test\b[^\n]*--project[= ]{_ROW})", re.IGNORECASE)
VPS_REASON = (
    "Heavy runs (test suites, browsers, builds, Blender) go to the VPS, not the Mac (Dom, 2026-10-08): "
    "ssh -i ~/.ssh/binance_futures_tool root@49.12.7.18 'capture --prio 3 <lane> <cmd>' (vps-heavy-jobs skill). "
    "Safari/WebKit, real-iPhone and sparring rows are exempt; if the VPS is down, prefix the command with MAC_HEAVY_OK=1."
)


HATCH_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*MAC_HEAVY_OK=1\b")
HATCH_LOG = Path.home() / ".claude" / "logs" / "deploy_guard.log"


def _runs(command: str) -> str:
    runs = executable_text(command) if command else ""
    return "\n".join(seg for seg in SEGMENT_SPLIT_RE.split(runs) if seg.strip() and not ALLOW_RE.search(seg)) if runs else ""


def note_hatch_use(command: str, cwd: str) -> bool:
    """One audit line per MAC_HEAVY_OK=1 use (Optimizer's condition on #104: without it the prefix becomes the habit)."""
    if not any(HEAVY_RE.search(seg) and HATCH_RE.search(seg) for seg in _runs(command).split("\n")):
        return False
    HATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(HATCH_LOG, "a") as f:
        f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} MAC_HEAVY_OK cwd={cwd} {command[:300]!r}\n")
    return True


def decide(command: str, lock: dict | None, own_pid: int | None = None) -> str:
    """Return the deny reason, or '' to allow."""
    runs = _runs(command)
    pixelmator = bool(lock) and bool(runs) and APP_HOST_RE.search(runs) is not None and PIXELMATOR_RE.search(command) is not None
    if not runs or not (HEAVY_RE.search(runs) or pixelmator):
        return ""
    if not lock:
        heavy = [seg for seg in runs.split("\n") if HEAVY_RE.search(seg)]
        return "" if all(MAC_EXEMPT_RE.search(seg) for seg in heavy) else VPS_REASON
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
    lock = active_lock()
    reason = decide(command, lock, own_pid=os.getpid())
    if not reason:
        if not lock:
            note_hatch_use(command, str(payload.get("cwd") or ""))
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason},
    }))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PreToolUse: warn a lane that is about to change the LIVE Claude hooks in ~/.claude/hooks.

Live is installed from the repo (DomLynch/Codex-Hooks, claude/hooks/), so an edit made only in the live copy is
undone by the next install. 2026-09-26: the Auditer fixed deploy_guard and codegraph_freshness straight in
~/.claude/hooks; both were live-only until #26/#27. The warning never blocks (an emergency hotfix may be right),
it tells the lane to put the same change in the repo. Sessions inside the Codex Hooks repo are the installers.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.api import exit_if_audit_child  # noqa: E402
from lib.log import log  # noqa: E402

COMPONENT = "live_hooks_guard"
LIVE_DIR = Path.home() / ".claude" / "hooks"
# The LIVE folder only (~, $HOME or the absolute home path), never a repo's own .claude/hooks/ project hooks.
HOOKS = r"(?:~|\$HOME|\$\{HOME\}|" + re.escape(str(Path.home())) + r")/\.claude/hooks"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# A shell segment that writes INTO the live folder: a redirect, sed -i, or a copy/move whose target is there, or a
# script run from `cd ~/.claude/hooks` that writes files (the Auditer's `python3 - <<EOF ... write_text` form).
REDIRECT_RE = re.compile(r"(?<![0-9&])>>?\s*['\"]?" + HOOKS + "/")
SED_RE = re.compile(r"\bsed\s+(?:-\w*\s+)*-i\b.*" + HOOKS + "/")
TARGET_RE = re.compile(r"^['\"]?" + HOOKS + r"(?:/|['\"]?$)")
COPY_RE = re.compile(r"^\s*(?:cp|mv|install|rsync|ln|tee)\b")
CD_LIVE_RE = re.compile(r"\bcd\s+['\"]?" + HOOKS + r"\b")
SCRIPT_WRITE_RE = re.compile(r"write_text\(|write_bytes\(|open\((?!\s*['\"]/dev/null)[^)]*['\"][wa]b?['\"]|\bsed\s+-i\b")


def live_write(tool_name: str, tool_input: dict) -> str:
    """The live path (or 'a live hook file') this call would change; '' when it changes nothing there."""
    if tool_name in EDIT_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        try:
            Path(path).expanduser().resolve().relative_to(LIVE_DIR.resolve())
        except (ValueError, OSError):
            return ""
        return path
    if tool_name != "Bash":
        return ""
    command = str(tool_input.get("command") or "")
    if ".claude/hooks" not in command:
        return ""
    for segment in re.split(r"&&|\|\||;|\n|\|", command):
        if REDIRECT_RE.search(segment) or SED_RE.search(segment):
            return segment.strip()[:120]
        if COPY_RE.match(segment) and TARGET_RE.match((segment.split() or [""])[-1]):
            return segment.strip()[:120]
    if CD_LIVE_RE.search(command) and SCRIPT_WRITE_RE.search(command):
        return "a live hook file (script run from ~/.claude/hooks)"
    return ""


def warning(target: str) -> str:
    return (f"You are changing the LIVE Claude hooks ({target}). They are installed from the repo DomLynch/Codex-Hooks "
            "(claude/hooks/), so a change made only here is undone by the next install. If this change is right, also put "
            "it and its test in a PR to that repo, or send it to the 'Hook scripts claude / codex' lane to land.")


def main() -> None:
    exit_if_audit_child()
    try:   # a reminder, never a failure: any odd payload means say nothing
        payload = json.loads(sys.stdin.read() or "{}")
        if "Codex Hooks" in str(payload.get("cwd") or ""):   # the hooks lane installs from the repo: not drift
            return
        tool_input = payload.get("tool_input")
        target = live_write(str(payload.get("tool_name") or ""), tool_input if isinstance(tool_input, dict) else {})
    except Exception:
        return
    if not target:
        return
    log(COMPONENT, f"warned cwd={payload.get('cwd')} target={target}")
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": warning(target)}}))


if __name__ == "__main__":
    main()

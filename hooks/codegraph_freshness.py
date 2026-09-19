#!/usr/bin/env python3
"""Refresh an indexed worktree immediately before CodeGraph reads it."""

import json
import subprocess
import sys
from pathlib import Path

from lib.git import git_root


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("tool_name") != "mcp__codegraph__codegraph_explore":
        return
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    path = Path(tool_input.get("projectPath") or payload.get("cwd") or ".").expanduser().resolve()
    root = git_root(path)
    if not root or not (root / ".codegraph" / "codegraph.db").is_file():
        return
    try:
        result = subprocess.run(["codegraph", "sync", "-q"], cwd=root, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return
        detail = f"exit {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = type(exc).__name__
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"CodeGraph refresh failed ({detail}) in {root}; retry sync before using its index.",
    }}))


if __name__ == "__main__":
    main()

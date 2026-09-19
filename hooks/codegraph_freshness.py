#!/usr/bin/env python3
"""Refresh an indexed worktree immediately before CodeGraph reads it."""

import json
import subprocess
import sys
from pathlib import Path


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
    for root in (path, *path.parents):
        if (root / ".codegraph" / "codegraph.db").is_file():
            break
    else:
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

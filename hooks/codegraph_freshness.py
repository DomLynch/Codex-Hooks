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
    project_path = tool_input.get("projectPath")
    if not isinstance(project_path, str) or not project_path.strip():
        deny("CodeGraph needs projectPath set to the repository or worktree being queried.")
        return
    path = Path(project_path).expanduser().resolve()
    root = next((candidate for candidate in (path, *path.parents) if (candidate / ".git").exists()), None)
    if root is None:
        deny(f"CodeGraph projectPath is outside a Git repository: {path}.")
        return
    if not (root / ".codegraph" / "codegraph.db").is_file():
        deny(f"CodeGraph has no index in {root}; initialize that repository before querying it.")
        return
    try:
        result = subprocess.run(["codegraph", "sync", "-q"], cwd=root, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return
        detail = f"exit {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = type(exc).__name__
    deny(f"CodeGraph refresh failed ({detail}) in {root}; retry sync before using its index.")


def deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()

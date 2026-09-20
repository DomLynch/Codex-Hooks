#!/usr/bin/env python3
# ruff: noqa: E402
"""PreToolUse: refresh the CodeGraph index of the repo about to be read.

The MCP server's file watcher covers edits made inside this session; this covers
everything else (other sessions, Codex/Kimi, git pull) so codegraph_explore never
answers from a stale index. Mirrors hooks/codegraph_freshness.py on the Codex side.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.api import exit_if_audit_child
from lib.log import log

COMPONENT = "codegraph_freshness"
TOOL = "mcp__codegraph__codegraph_explore"
SYNC_TIMEOUT_S = 30


def index_root(path: Path) -> Path | None:
    for root in (path, *path.parents):
        if (root / ".codegraph" / "codegraph.db").is_file():
            return root
    return None


def deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def main() -> None:
    exit_if_audit_child()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict) or payload.get("tool_name") != TOOL:
        return
    tool_input = payload.get("tool_input") or {}
    raw = (tool_input.get("projectPath") if isinstance(tool_input, dict) else None) or payload.get("cwd") or "."
    root = index_root(Path(raw).expanduser().resolve())
    if root is None:
        return
    try:
        result = subprocess.run(["codegraph", "sync", "-q"], cwd=root, capture_output=True, text=True, timeout=SYNC_TIMEOUT_S)
        if result.returncode == 0:
            log(COMPONENT, f"synced {root}")
            return
        detail = f"exit {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = type(exc).__name__
    log(COMPONENT, f"sync failed ({detail}) in {root}")
    deny(f"CodeGraph refresh failed ({detail}) in {root}; retry `codegraph sync` before using its index.")


if __name__ == "__main__":
    main()

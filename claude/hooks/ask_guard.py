#!/usr/bin/env python3
"""PreToolUse: deny AskUserQuestion in every lane.

2026-10-16 (Dom, via Strategy): PicoClaw froze overnight on a blocking "stop the Codex jobs? yes/no" pop-up that
Dom could not click while away. A lane must pick the conservative default and say so in text instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.api import AUDIT_CHILD_ENV, exit_if_audit_child  # noqa: E402,F401
from lib.log import log  # noqa: E402

COMPONENT = "ask_guard"
REASON = ("No blocking questions (Dom 10-16). Pick the conservative default, write the question and your default in "
          "one line to Dom in your next message and in your state doc, and continue.")


def main() -> None:
    exit_if_audit_child()
    try:   # any odd payload means allow: a guard must never be the thing that breaks a lane
        payload = json.loads(sys.stdin.read() or "{}")
        if payload.get("tool_name") != "AskUserQuestion":
            return
    except Exception:
        return
    log(COMPONENT, f"denied cwd={payload.get('cwd')}")
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": REASON}}))


if __name__ == "__main__":
    main()

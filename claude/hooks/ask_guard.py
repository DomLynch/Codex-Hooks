#!/usr/bin/env python3
"""PreToolUse: deny AskUserQuestion in every lane.

2026-10-16 (Dom, via Strategy): PicoClaw froze overnight on a blocking "stop the Codex jobs? yes/no" pop-up that
Dom could not click while away. A lane must pick the conservative default instead; ~00:4x Dom ruled that questions never go to him: a ruling
comes from Lead (operations) or Strategy (scope) by message.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.log import log  # noqa: E402

COMPONENT = "ask_guard"
REASON = ("No blocking questions (Dom 10-16). Pick the conservative default and continue. If you need a ruling, send "
          "one line to 'Frankendom - Lead Dev' (operations, order of work) or 'Project Strategy - Fable 5.1' (scope, "
          "acceptance) by message and keep working on your next item; never wait on Dom.")


def main() -> None:
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

#!/usr/bin/env python3
"""PreToolUse: deny AskUserQuestion, and wrap_up calls that carry a question or buttons, in every lane.

2026-10-16 (Dom, via Strategy): a lane froze overnight on a blocking "stop the Codex jobs? yes/no" pop-up that
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


# 2026-10-17 (Strategy, Dom's screenshot): the app's wrap_up tool renders `ask` + `actions` as a question card with buttons, the same
# pop-up by another tool name. Deny only that shape; a recap alone passes.
WRAP_UP = "mcp__ccd_turn__wrap_up"
WRAP_UP_REASON = ("No blocking questions (Dom 10-16): this covers any tool that renders a question or buttons to Dom (AskUserQuestion, "
                  "wrap_up ask/actions, and any future one). Call wrap_up again with the recap only, no `ask` and no `actions`. Pick the "
                  "conservative default and continue; if you need a ruling, send one line to 'Frankendom - Lead Dev' or "
                  "'Project Strategy - Fable 5.1' by message; never wait on Dom.")


def main() -> None:
    try:   # any odd payload means allow: a guard must never be the thing that breaks a lane
        payload = json.loads(sys.stdin.read() or "{}")
        tool = payload.get("tool_name")
        reason = REASON
        if tool == WRAP_UP:
            tool_input = payload.get("tool_input") or {}
            if not (tool_input.get("ask") or tool_input.get("actions")):
                return   # a plain recap is fine; only the question card is refused
            reason = WRAP_UP_REASON
        elif tool != "AskUserQuestion":
            return
    except Exception:
        return
    log(COMPONENT, f"denied {tool} cwd={payload.get('cwd')}")
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()

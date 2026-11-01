#!/usr/bin/env python3
"""PreToolUse on Agent: append the standing heavy-job line to a sub-agent brief that mentions heavy work.

2026-10-29 (Strategy + Dom): the VPS hit load 68 from lane suites and builds. 2026-10-31 (Dom via Strategy, VPS restructure): the Windows laptop is the heavy-job host and the VPS is
production only (this replaces the hfjob / Hugging Face CPU rule). Lanes spawn sub-agents with ad-hoc prompts (no shared template exists), so the Agent tool call itself is
rewritten: when the prompt mentions test / suite / build / playwright / stills / blender, `updatedInput` carries the same prompt plus the standing line. A prompt that never
mentions heavy work, or already carries the line, passes untouched. The hook rewrites the brief only; it never blocks a spawn.
"""
from __future__ import annotations

import json
import re
import sys

LINE = ("Suites, builds, Playwright, stills, clips and Blender run on the Windows laptop, one heavy job at a time, in a checkout there; never on the VPS "
        "(production only: no worktrees, no scratch, no heavy jobs, no pkill by name, never touch armagedom paths). "
        "GitHub is the source of truth: commit and push your branch (Dom + Strategy 2026-10-31, VPS restructure).")
# A request to run something heavy. `test-driven`, `contest` and other words that merely contain the stem do not count; hyphenated compounds are not run requests.
HEAVY = re.compile(r"(?<![\w-])(?:tests?|testing|suites?|builds?|building|playwright|vitest|stills?|blender|vite)(?![\w-])", re.IGNORECASE)


def updated_input(tool_input: dict) -> dict | None:
    prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else None
    if not isinstance(prompt, str) or not prompt or LINE in prompt or not HEAVY.search(prompt):
        return None
    return {**tool_input, "prompt": f"{prompt.rstrip()}\n\n{LINE}"}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    if payload.get("tool_name") != "Agent":
        return
    new = updated_input(payload.get("tool_input") or {})
    if new is None:
        return
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": new}}))


if __name__ == "__main__":
    main()

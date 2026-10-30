#!/usr/bin/env python3
"""PreToolUse on Agent: append the standing hfjob line to a sub-agent brief that mentions heavy work.

2026-10-29 (Strategy + Dom): the VPS hit load 68 from lane suites and builds; the ruling is "all suites/builds move to Hugging Face CPU, VPS = live sites only". Lanes spawn
sub-agents with ad-hoc prompts (no shared template exists), and the sub-agents never heard of hfjob. So the Agent tool call itself is rewritten: when the prompt mentions
test / suite / build / playwright / stills / blender, `updatedInput` carries the same prompt plus the standing line. A prompt that never mentions heavy work, or already names
hfjob, passes untouched. The hook rewrites the brief only; it never blocks a spawn.
"""
from __future__ import annotations

import json
import re
import sys

LINE = ("Suites and builds via hfjob -- <cmd>; never on the VPS. Browser/WebGL proof rows (Playwright, stills, clips) run on the Mac when its load is under 12, one at a time, "
        "else on the HF T4 for one final proof (HF CPU cannot prove GPU rows). "
        "Clone with --filter=blob:none --depth 1 --single-branch (Dom + Strategy 2026-10-29, GPU waterfall Dom 2026-10-17). "
        # 2026-10-30 (Strategy RULING A via Micro-Manager): hfjob clones over public https; opentrident-steam is private ('Invalid username or token') and no new secret is added.
        "hfjob is for PUBLIC repos only; in a private repo run light tests on the Mac, one targeted file per call (node --test <file>, no glob or directory).")
# A request to run something heavy. `test-driven`, `contest` and other words that merely contain the stem do not count; hyphenated compounds are not run requests.
HEAVY = re.compile(r"(?<![\w-])(?:tests?|testing|suites?|builds?|building|playwright|vitest|stills?|blender|vite)(?![\w-])", re.IGNORECASE)


def updated_input(tool_input: dict) -> dict | None:
    prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else None
    if not isinstance(prompt, str) or not prompt or "hfjob" in prompt or not HEAVY.search(prompt):
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

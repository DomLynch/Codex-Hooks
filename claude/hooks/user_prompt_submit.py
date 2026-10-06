#!/usr/bin/env python3
# ruff: noqa: E402
"""Claude Code UserPromptSubmit hook — injects autonomy contract + V4/MCP routing.

Mirrors ~/.codex/hooks/user_prompt_submit.py but:
  - Reads Claude Code payload (`prompt`, `cwd`, `session_id`, `transcript_path`)
  - Outputs in Claude Code's UserPromptSubmit additional-context format
  - Logs to ~/.claude/logs/user_prompt_submit.log
"""
# ruff: noqa: E402
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import active_lessons, lesson_lane
from lib.json_state import update_json_locked
from lib.context_size import context_notice
from lib.handoff_check import prompt_handoff_notice
from lib.inbox import claim_for, render as render_inbox
from lib.api import exit_if_audit_child
from lib.log import log
from lib.policies import AUTONOMY_POLICY, CONTEXT_EFFICIENCY_POLICY, WORK_QUALITY_POLICY, is_project_work


COMPONENT = "user_prompt_submit"

# The static policy text is identical on every prompt and stays in the model's context once injected.
# Measured 2026-09-22: 1,791 injections that day, 1,405 of them the full ~2,400-token project contract - about
# 3.4M tokens of the same words re-sent. Send it in full on a session's first project prompt and every
# REFRESH_EVERY prompts after (compaction can drop it); send a short pointer in between. Active Lessons change
# per turn and are always sent in full.
POLICY_STATE = Path.home() / ".claude" / "state" / "prompt_policy.json"
REFRESH_EVERY = 15
POINTER = ("# V4/MCP Task Routing Contract\n"
           "The full contract was injected earlier in this session and still applies: autonomy (act on reversible "
           "defaults, ask only on true blockers), release close-out, finding closure, work quality (smallest "
           "surgical change, run .quality-gate.json, receipts not claims), context efficiency (lean output, "
           "Semble + Codegraph before non-trivial edits, scope to the current task).")


def policy_due(session_id: str) -> bool:
    """True when the full contract should be re-sent (first project prompt, or every REFRESH_EVERY)."""
    if not session_id:
        return True
    try:
        def updater(data: dict) -> dict:
            n = int(data.get(session_id, 0)) + 1
            data[session_id] = n
            if len(data) > 60:
                data = dict(list(data.items())[-60:])
            return data
        after = update_json_locked(POLICY_STATE, updater)
        return (int(after.get(session_id, 1)) - 1) % REFRESH_EVERY == 0
    except Exception:
        return True


def with_context_notice(context: str, transcript_path: str, cwd: Path) -> str:
    """Append (or, past the urgent threshold, prepend) the restart nudge. One line, no model call, no blocking."""
    line, put_first = context_notice(transcript_path, str(cwd))
    if not line:
        return context
    try:
        handoff = prompt_handoff_notice(transcript_path, cwd)
    except Exception as exc:   # the report must never cost the prompt its context
        where = traceback.extract_tb(exc.__traceback__)[-1]   # 2026-10-06: "division by zero" x5 named no line
        log(COMPONENT, f"handoff check failed: {exc} at {Path(where.filename).name}:{where.lineno} in {where.name}")
        handoff = ""
    if handoff:
        line = f"{line}\n{handoff}"
    return f"{line}\n\n{context}" if put_first else f"{context}\n\n{line}"


def context_for(prompt: str, cwd: Path, session_id: str = "", transcript_path: str = "") -> str:
    lessons = active_lessons(lesson_lane(transcript_path, str(cwd), session_id))
    lessons_context = ""
    if lessons:
        lessons_context = "\n\n# Active Lessons\n" + "\n".join(f"- {lesson}" for lesson in lessons)
    if not is_project_work(prompt):
        return (
            "# Current-Prompt Discipline\n"
            "- Treat this as the current task only; do not trawl old history unless needed.\n"
            "- For simple chat, answer directly and keep it concise.\n"
            "- If the prompt turns into project/code work, switch to the V4/MCP routing contract.\n\n"
            f"{AUTONOMY_POLICY}{lessons_context}"
        )

    if not policy_due(session_id):
        return "\n".join([POINTER, f"- Current cwd from hook: `{cwd}`.", lessons_context.strip()])

    return "\n".join(
        [
            "# V4/MCP Task Routing Contract",
            "Apply before acting.",
            AUTONOMY_POLICY,
            WORK_QUALITY_POLICY,
            CONTEXT_EFFICIENCY_POLICY,
            "- Scope to current task; do not trawl history unless needed.",
            "- Project/code/repo work: call knowledge MCP `get_playbook()` when available.",
            "- Target repo known: read local `AGENTS.md`; also `PROJECT_STATE.md` when present.",
            "- Route tools deliberately: knowledge MCP for doctrine, GitHub for PR/CI, Browser for UI/local web, official docs/web for current external facts.",
            "- Mandatory before non-trivial edits: use Semble and Codegraph together, not as alternatives; run three materially different Semble searches, then Codegraph MCP for structural context/impact. If skipping either, state why.",
            "- Before stopping, give a concise coverage receipt for relevant entry points, state/config, retries/fallbacks/background work, tests, and post-edit impact/index freshness.",
            "- Scale V4 loop to task: success condition, likely files, smallest reversible change, strong verification.",
            "- Before stopping, give evidence: files touched, checks/logs run, remaining risk.",
            f"- Current cwd from hook: `{cwd}`.",
            lessons_context.strip(),
        ]
    )


def emit_context(context: str) -> None:
    """Claude Code UserPromptSubmit additional-context output format."""
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )


def with_inbox(context: str, cwd: Path) -> str:
    """Prepend any messages other agents (Codex) left for this session. Fail-open: an inbox problem never blocks a
    prompt, it just delivers nothing, and the message stays in the inbox for the next prompt."""
    try:
        messages = claim_for(cwd)
    except Exception as exc:
        log(COMPONENT, f"inbox fail-open: {exc}")
        return context
    if not messages:
        return context
    log(COMPONENT, f"inbox delivered {len(messages)} message(s) from {sorted({str(m.get('from')) for m in messages})}")
    return f"{render_inbox(messages)}\n\n{context}"


def main() -> None:
    exit_if_audit_child()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        prompt = str(payload.get("prompt") or "")
        cwd = Path(payload.get("cwd") or ".").resolve()
        mode = "project" if is_project_work(prompt) else "simple"
        log(COMPONENT, f"context injected mode={mode} prompt_chars={len(prompt)} cwd={cwd}")
        context = context_for(prompt, cwd, str(payload.get("session_id") or ""), str(payload.get("transcript_path") or ""))
        emit_context(with_inbox(with_context_notice(context, str(payload.get("transcript_path") or ""), cwd), cwd))
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

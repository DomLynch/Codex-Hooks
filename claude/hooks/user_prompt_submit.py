#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — injects autonomy contract + V4/MCP routing.

Mirrors ~/.codex/hooks/user_prompt_submit.py but:
  - Reads Claude Code payload (`prompt`, `cwd`, `session_id`, `transcript_path`)
  - Outputs in Claude Code's UserPromptSubmit additional-context format
  - Logs to ~/.claude/logs/user_prompt_submit.log
"""
# ruff: noqa: E402
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.log import log


COMPONENT = "user_prompt_submit"

AUTONOMY_POLICY = "\n".join(
    [
        "# Autonomous Execution Contract",
        "- Default to action. Do not ask for confirmation, permission, or clarification when a reasonable reversible default exists.",
        "- If details are missing, choose the conservative project-local default, state the assumption briefly, and keep working.",
        "- Ask the user only for true blockers: credentials/secrets, irreversible destructive actions, production/live external side effects, purchases/payments, legal/medical/financial decisions, or mutually exclusive product choices with high blast radius.",
        "- Never stop just to ask whether to continue, whether to run ordinary checks, whether to inspect local files, whether to apply a clear fix, or whether to proceed with the next queued task.",
        "- For long autonomous runs, prefer bounded reversible progress with logs, checkpoints, and rollback paths over waiting for micro-management.",
    ]
)

PROJECT_WORDS = re.compile(
    r"\b("
    r"implement|fix|build|edit|change|patch|commit|push|deploy|test|debug|review|"
    r"repo|project|code|hook|mcp|playbook|github|pr|ci|vps|server|app|agent|bot"
    r")\b",
    re.I,
)


def is_project_work(prompt: str) -> bool:
    if PROJECT_WORDS.search(prompt):
        return True
    return bool(re.search(r"[\w./ -]+\.(py|ts|tsx|js|go|rs|md|toml|json|yaml|yml)\b", prompt))


def context_for(prompt: str, cwd: Path) -> str:
    if not is_project_work(prompt):
        return (
            "# Current-Prompt Discipline\n"
            "- Treat this as the current task only; do not trawl old history unless needed.\n"
            "- For simple chat, answer directly and keep it concise.\n"
            "- If the prompt turns into project/code work, switch to the V4/MCP routing contract.\n\n"
            f"{AUTONOMY_POLICY}"
        )

    return "\n".join(
        [
            "# V4/MCP Task Routing Contract",
            "Apply this to the current user prompt before acting.",
            AUTONOMY_POLICY,
            "- Keep scope to the current task; do not trawl months of history unless the task needs it.",
            "- For project/code/repo work, call knowledge MCP `get_playbook()` when available.",
            "- If the target repo is known, read its local `AGENTS.md`; also read `PROJECT_STATE.md` when present.",
            "- Route the toolkit deliberately: knowledge MCP for project doctrine, GitHub for PR/CI, Browser for UI/local web, official docs/web only when current external facts matter.",
            "- Use the V4 core loop scaled to task size: success condition, likely files, smallest reversible change, strong relevant verification.",
            "- Before stopping, give evidence: files touched, checks/logs run, remaining risk.",
            f"- Current cwd from hook: `{cwd}`.",
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


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        prompt = str(payload.get("prompt") or "")
        cwd = Path(payload.get("cwd") or ".").resolve()
        mode = "project" if is_project_work(prompt) else "simple"
        log(COMPONENT, f"context injected mode={mode} prompt_chars={len(prompt)} cwd={cwd}")
        emit_context(context_for(prompt, cwd))
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

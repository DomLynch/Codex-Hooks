#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import active_lessons
from lib.log import log


COMPONENT = "user_prompt_submit"

AUTONOMY_POLICY = "\n".join(
    [
        "# Autonomous Execution Contract",
        "- Default to action; Do not ask for confirmation when a reasonable reversible default exists.",
        "- Missing detail: choose the conservative project-local default, state it briefly, keep working.",
        "- Ask the user only for true blockers: secrets, destructive/irreversible actions, live external side effects, purchases, regulated decisions, or high-blast-radius product choices.",
        "- Never stop just to ask whether to continue, run ordinary checks, inspect files, apply a clear fix, or proceed with the next queued task.",
        "- Long runs: make bounded reversible progress with logs/checkpoints instead of waiting for micro-management.",
    ]
)

WORK_QUALITY_POLICY = "\n".join(
    [
        "# General Work Quality Contract",
        "- Prefer the smallest surgical change that solves the root problem; avoid speculative abstractions, broad rewrites, and file/module sprawl.",
        "- Prefer minimal LOC: delete/refactor before adding; avoid hardcoded or silo-specific fixes unless scoped.",
        "- Use boring explicit code. When debugging, reproduce or cite the concrete failure, localize, fix minimally, verify gone.",
        "- Respect mode: audit-only means no edits; implementation means changed files and checks.",
        "- Before done: two-pass review plus strongest relevant automated/judge check.",
        "- Code changes: run the repository .quality-gate.json commands; never skip missing tools, regenerate baselines to pass, or report a failed check as success.",
        "- Debugging: require a reproducer and regression test. Use ast-grep for structural bug patterns, Hypothesis/randomized tests for state/order bugs, VizTracer/py-spy for slowness/hangs, and bounded mutation tests for suspect assertions.",
        "- Use Tree-sitter miswire audit when Codegraph edges are disputed and a bounded, secret-free Repomix pack for cross-agent handoffs. Report applicable tool evidence or a concrete reason it cannot run; do not run unrelated diagnostics.",
    ]
)

CONTEXT_EFFICIENCY_POLICY = "\n".join(
    [
        "# Context Efficiency Contract",
        "- Keep command output lean: use targeted commands, capped output, summarized failures, and narrow paths.",
        "- No full diffs/logs/dependency trees/generated artifacts/archives/repeated output unless explicitly needed.",
        "- For non-trivial work, use Semble and Codegraph together, not as alternatives: Semble for discovery, Codegraph for structure/impact, and `rg` for literal text.",
        "- If Codegraph is unavailable, retry once; only then degrade to Semble + `rg` and state the fallback in the receipt.",
        "- Avoid duplicate parallel exploration; split agents by scout/surgeon/reviewer roles with non-overlapping scope.",
        "- Start substantial project work from a compact context packet: task, known files, do-not-inspect paths, evidence, success checks.",
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
    lessons = active_lessons()
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

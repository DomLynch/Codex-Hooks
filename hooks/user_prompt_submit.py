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
from lib.context_size import context_notice
from lib.log import log


COMPONENT = "user_prompt_submit"

RELEASE_POLICY = """# Release Close-out
- Major implementation includes required checks/review, scoped commit/push, the configured deployment workflow and live verification. Honor local-only/no-commit/no-push/no-deploy, audit/review-only and stop requests; no configured deploy target means not applicable. Never invent targets or bypass failed safeguards.
- Verify git status --porcelain, local/remote/deployed parity, active config/service and affected user-visible behavior; matching SHAs alone are insufficient. Report blockers and partial sync honestly.
- Commit only task-owned changes. Preserve unrelated work; never delete/stash/commit it for cleanliness or create an empty commit when the change is already committed.
"""

COMPLETION_POLICY = """# Finding Closure
- Keep current implementation failures open until the same check/workflow passes on changed code or evidence proves it expected/unrelated. Record the failure, disposition and confirming receipt. Reporting or delegation is not a fix; no unrelated green checks, weakened assertions, deleted checks or raised baselines.
- Put repeatable browser/workflow checks in .quality-gate.json completion_commands as argv lists; no production writes or paid calls in automatic checks.
- Verify deployed revision and affected behavior, including relevant older issues and logs. Document historical issue dispositions; zero historical issues is not required.
- Honor stop/pause, audit-only, plan-only and status-only scope. A genuine blocker needs the failed check and owner/next step; avoid endless retries or unrelated repairs.
"""

AUTONOMY_POLICY = "\n".join(
    [
        "# Autonomous Execution Contract",
        "- Default to action; Do not ask for confirmation when a reasonable reversible default exists.",
        "- Missing detail: choose the conservative project-local default, state it briefly, keep working.",
        "- Existing authorization covers ordinary necessary steps within scope, including a requested deployment; do not ask for the same permission twice.",
        "- Ask the user only for true blockers: a genuine missing input after checking approved sources, or an unapproved destructive/irreversible action, purchase, regulated decision, or high-blast-radius scope choice. Never expose secrets.",
        "- A simple, reversible fix within scope: apply it and verify it, rather than asking whether to continue. Do not start unrelated backlog tasks.",
        "- Finish the authorized outcome end-to-end, including requested deployment and live checks; local tests alone are not completion. An honest unfinished summary is not a reason to stop while executable work remains.",
        "- If one step is blocked, finish independent authorized work, then report the failed command/check, exact missing input and what remains. Never invent credentials or bypass failed safety checks.",
        "- Explicit stop/pause, audit-only, plan-only and status-only requests override persistence. Retry exhaustion is unresolved work, not success; avoid infinite retries.",
        "- Long runs: make bounded reversible progress with logs/checkpoints instead of waiting for micro-management.",
        RELEASE_POLICY,
        COMPLETION_POLICY,
    ]
)

WORK_QUALITY_POLICY = "\n".join(
    [
        "# General Work Quality Contract",
        "- Prefer the smallest surgical change and minimal LOC; delete/refactor before adding. Avoid speculative abstractions, broad rewrites, and file/module sprawl.",
        "- Use boring explicit code. When debugging, reproduce or cite the concrete failure, localize, fix minimally, verify gone.",
        "- Respect mode: audit-only means no edits; implementation means changed files and checks.",
        "- Before done: two-pass review plus strongest relevant automated/judge check.",
        "- Code changes: run the repository .quality-gate.json commands; never skip missing tools, regenerate baselines to pass, or report a failed check as success.",
        "- Debugging: require a reproducer and regression test. Use ast-grep for structural bug patterns, Hypothesis/randomized tests for state/order bugs, VizTracer/py-spy for slowness/hangs, and bounded mutation tests for suspect assertions.",
        "- Use Tree-sitter miswire audit when call-graph edges are disputed and a bounded, secret-free Repomix pack for cross-agent handoffs. Report applicable tool evidence or a concrete reason it cannot run; do not run unrelated diagnostics.",
    ]
)

CONTEXT_EFFICIENCY_POLICY = "\n".join(
    [
        "# Context Efficiency Contract",
        "- Keep command output lean: use targeted commands, capped output, summarized failures, and narrow paths.",
        "- No full diffs/logs/dependency trees/generated artifacts/archives/repeated output unless explicitly needed.",
        "- For non-trivial work, use Semble for discovery and `rg` for literal text; say why if you skip Semble.",
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
    if not is_project_work(prompt):
        return (
            "# Current-Prompt Discipline\n"
            "- Treat this as the current task only; do not trawl old history unless needed.\n"
            "- For simple chat, answer directly and keep it concise.\n"
            "- If the prompt turns into project/code work, switch to the V4/MCP routing contract.\n\n"
            + "# Autonomous Execution Contract\n"
            "- Use existing authorization and reasonable reversible defaults; do not ask whether to continue.\n"
            "- Ask the user only for true blockers or actions outside that authorization.\n"
            "- Respect explicit stop/pause, audit-only and status-only scope; do not resume unrelated work.\n"
        )

    lessons = active_lessons(cwd=cwd)
    lessons_context = ""
    if lessons:
        lessons_context = (
            "# Active Lessons (past examples, not open tasks)\n"
            "- Apply only when relevant to this request; never resume an old project's work.\n"
            + "\n".join(f"- {lesson}" for lesson in lessons)
        )

    return "\n".join(
        [
            "# V4/MCP Task Routing Contract",
            "Apply before acting.",
            AUTONOMY_POLICY,
            WORK_QUALITY_POLICY,
            CONTEXT_EFFICIENCY_POLICY,
            "- Target repo known: read local `AGENTS.md`; also `PROJECT_STATE.md` when present.",
            "- Route tools deliberately: GitHub for PR/CI, Browser for UI/local web, official docs/web for current external facts.",
            "- Before non-trivial edits, find code with Semble (search by what the code does, then read the file at the returned line) and `rg` for literal text; if you skip Semble, say why.",
            "- Before stopping, give a concise coverage receipt for relevant entry points, state/config, retries/fallbacks/background work, tests, and post-edit impact/index freshness.",
            "- Scale V4 loop to task: success condition, likely files, smallest reversible change, strong verification.",
            "- Before stopping, give evidence: files touched, checks/logs run, remaining risk.",
            f"- Current cwd from hook: `{cwd}`.",
            lessons_context,
        ]
    )


def with_context_notice(context: str, transcript_path: str, cwd: Path) -> str:
    """Append (or, past the urgent threshold, prepend) the restart nudge. One line, no model call, no blocking.
    Fail-open: a payload with no transcript_path, or a transcript with no usage record yet, adds nothing."""
    try:
        line, put_first = context_notice(transcript_path, str(cwd))
    except Exception:
        return context
    if not line:
        return context
    return f"{line}\n\n{context}" if put_first else f"{context}\n\n{line}"


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
        emit_context(with_context_notice(
            context_for(prompt, cwd), str(payload.get("transcript_path") or ""), cwd))
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

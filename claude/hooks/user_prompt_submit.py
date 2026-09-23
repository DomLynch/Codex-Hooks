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
import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import active_lessons
from lib.json_state import update_json_locked
from lib.context_size import context_notice
from lib.inbox import claim_for, render as render_inbox
from lib.api import exit_if_audit_child
from lib.log import log


COMPONENT = "user_prompt_submit"

RELEASE_POLICY = """# Release Close-out
- After major code changes, complete required checks/review, scoped commit/push, the repository's configured deployment workflow, and live verification before declaring done.
- This is the default for implementation, not permission to invent deployment targets or bypass release safeguards. Explicit local-only, no-commit, no-push, no-deploy, review-only and stop requests override it; no configured deployment is a valid not-applicable case.
- Verify git status --porcelain, local/remote/deployed revision or artifact parity, active config/service and relevant user-visible behavior. A clean checkout or matching SHA alone is not proof the release is live. Report exact blockers or exceptions; never call partial sync clean.
- Commit only task-owned changes; do not delete, stash or commit unrelated work to manufacture no dirt. Isolate or coordinate safely. If the correct change is already committed, verify it rather than creating an empty commit.
"""

COMPLETION_POLICY = """# Finding Closure
- On implementation tasks, keep current-task failures open until the same check/workflow passes on the changed code, or concrete evidence establishes an expected/unrelated result. Track check/resource, failing receipt, disposition and confirming receipt in task notes.
- Reporting, delegating, acknowledging or resolving a Sentry issue is not a fix. Do not substitute unrelated green tests, weakened assertions, deleted checks or raised baselines for closure.
- Put repeatable browser/workflow checks in .quality-gate.json completion_commands (argv lists) so Stop runs them alongside commands; manual check descriptions alone are not executable gates. No production writes or paid calls in automatic checks.
- Before claiming live resolution, verify the deployed revision and affected workflow; inspect relevant older Sentry issues as well as the latest release. Do not require zero historical issues: document evidence-based dispositions.
- Preserve explicit stop/pause, audit-only, plan-only and status-only modes. A genuine access/input/ownership blocker needs its failed check and owner/next step; do not loop forever or start unrelated repairs.
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

SENTRY_POLICY = "- For production incidents, consult the project's Sentry errors when configured and accessible, then reproduce locally. Cite the project/environment/release and event receipt; unavailable Sentry is not a clean bill of health. Never upload secrets, prompts or private user content."

WORK_QUALITY_POLICY = "\n".join(
    [
        "# General Work Quality Contract",
        "- Prefer the smallest surgical change that solves the root problem; avoid speculative abstractions, broad rewrites, and file/module sprawl.",
        "- Prefer minimal LOC: delete/refactor before adding; avoid hardcoded or silo-specific fixes unless scoped.",
        "- Use boring explicit code. When debugging, reproduce or cite the concrete failure, localize, fix minimally, verify gone.",
        "- Respect mode: audit-only means no edits; implementation means changed files and checks.",
        SENTRY_POLICY,
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
    return f"{line}\n\n{context}" if put_first else f"{context}\n\n{line}"


def context_for(prompt: str, cwd: Path, session_id: str = "") -> str:
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
        context = context_for(prompt, cwd, str(payload.get("session_id") or ""))
        emit_context(with_inbox(with_context_notice(context, str(payload.get("transcript_path") or ""), cwd), cwd))
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

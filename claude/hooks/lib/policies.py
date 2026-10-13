"""Policy text shared by the prompt and session-start hooks, and the test for "is this prompt project work"."""
from __future__ import annotations

import re


RELEASE_POLICY = """# Release Close-out
- After major code changes, complete required checks/review, scoped commit/push, the repository's configured deployment workflow, and live verification before declaring done.
- This is the default for implementation, not permission to invent deployment targets or bypass release safeguards. Explicit local-only, no-commit, no-push, no-deploy, review-only and stop requests override it; no configured deployment is a valid not-applicable case.
- Verify git status --porcelain, local/remote/deployed revision or artifact parity, active config/service and relevant user-visible behavior. A clean checkout or matching SHA alone is not proof the release is live. Report exact blockers or exceptions; never call partial sync clean.
- Commit only task-owned changes; do not delete, stash or commit unrelated work to manufacture no dirt. Isolate or coordinate safely. If the correct change is already committed, verify it rather than creating an empty commit.
"""

COMPLETION_POLICY = """# Finding Closure
- On implementation tasks, keep current-task failures open until the same check/workflow passes on the changed code, or concrete evidence establishes an expected/unrelated result. Track check/resource, failing receipt, disposition and confirming receipt in task notes.
- Reporting, delegating or acknowledging an issue is not a fix. Do not substitute unrelated green tests, weakened assertions, deleted checks or raised baselines for closure.
- Put repeatable browser/workflow checks in .quality-gate.json completion_commands (argv lists) so Stop runs them alongside commands; manual check descriptions alone are not executable gates. No production writes or paid calls in automatic checks.
- Before claiming live resolution, verify the deployed revision and affected workflow; inspect relevant older issues and logs as well as the latest release. Do not require zero historical issues: document evidence-based dispositions.
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


# Dom, 2026-10-13 ("YES!!" to Hooks Dev in chat): lanes kept waiting on Dom after every /clear because the delegation lived only in conversations.
# Injected by session_start for every session; Lead's CLAUDE.md paragraph carries the same rule in the repo. Edit the words in ONE place.
DELEGATION_POLICY = """# Delegation (the same paragraph as CLAUDE.md, RPG-game#2499)
- **Dom's standing delegation (2026-10-13):** Lead Dev and Project Strategy carry Dom's authority for release, pipeline, CI, config, lane and order-of-work decisions in this repo, on the Mac and on the VPS. A written ruling from either is Dom's word; do not wait for Dom. Exceptions that still need Dom in chat: spending money, legal, wiping player data, parties/guilds. Dom's direct word to a lane overrides a relayed plan. Waiting on Dom for anything else is a defect; flag it to Lead.
"""


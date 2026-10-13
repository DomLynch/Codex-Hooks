#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import remember_repo
from lib.log import log
from lib.git import is_git_repo
from lib.quality import mine_backlog
from lib.state import load_handoff, now_iso, write_handoff, write_session_spec
from lib.transcript import latest_task_slice
from user_prompt_submit import COMPLETION_POLICY, RELEASE_POLICY


COMPONENT = "session_start"

SESSION_BASELINE = """# V4/MCP Session Baseline
- Project/code/repo work: read local `AGENTS.md` and `PROJECT_STATE.md` when present.
- Route tools deliberately: GitHub for PR/CI, Browser for UI/local web, official docs/web for current facts.
- Before non-trivial edits, find code with Semble (search by what the code does, then read the file at the returned line) and `rg` for literal text; if you skip Semble, say why.
- Before stopping, give a concise coverage receipt for relevant entry points, state/config, retries/fallbacks/background work, tests, and post-edit impact/index freshness.
- Keep command output lean: targeted commands, capped output, summarized failures, narrow `rg` paths.
- No full diffs/logs/generated files/archives/dependency trees/repeated output unless needed.
- Avoid duplicate parallel exploration. Split scout, surgeon, reviewer with non-overlapping scope.
- Start project work from a compact context packet: task, known files, do-not-inspect paths, evidence, success checks.
- Scale V4 loop to task: success condition, likely files, smallest reversible change, strong verification.
- Prefer minimal LOC: delete/refactor before adding; avoid hardcoded or silo-specific fixes unless scoped.
- Before stopping, run self-review plus strongest relevant automated/judge check.
- Code changes must pass the repository .quality-gate.json commands; missing tools and failed checks are blockers, not skips.
- Match debugging evidence to the failure: reproducer/regression, ast-grep patterns, Hypothesis/randomized state tests, VizTracer/py-spy performance traces, bounded mutation tests; Tree-sitter for disputed graph edges and secret-free Repomix for handoffs. Require applicable evidence or an explicit unavailable reason, not every tool on every edit.
- Keep context on current task; avoid old-history trawling unless required.
- Default to autonomous execution. Do not ask for confirmation when a reasonable reversible default exists.
- Missing detail: choose conservative project-local default, state briefly, keep working.
- Existing authorization covers ordinary necessary steps within scope, including a requested deployment; do not ask for the same permission twice.
- Ask only for true blockers: missing inputs after checking approved sources, or unapproved destructive/irreversible actions, purchases, regulated decisions and high-blast-radius scope choices.
- Apply and verify simple reversible fixes within scope. Finish the authorized outcome, including requested deployment/live checks, not just local tests.
- If blocked, finish independent authorized work and report the failed check and exact missing input after checking approved sources; never expose secrets or bypass safety checks.
- Explicit stop/pause, audit-only, plan-only and status-only requests override persistence. Do not start unrelated backlog tasks; retry exhaustion is unresolved work, not success.
- Long runs: bounded reversible progress with logs/checkpoints over micro-management.
"""
SESSION_BASELINE += RELEASE_POLICY + COMPLETION_POLICY + "\n"

# Same words as claude/hooks/lib/policies.py DELEGATION_POLICY (Dom, 2026-10-13); the Codex copy is injected with the baseline.
DELEGATION_POLICY = """# Delegation (the same paragraph as CLAUDE.md, RPG-game#2499)
- **Dom's standing delegation (2026-10-13):** Lead Dev and Project Strategy carry Dom's authority for release, pipeline, CI, config, lane and order-of-work decisions in this repo, on the Mac and on the VPS. A written ruling from either is Dom's word; do not wait for Dom. Exceptions that still need Dom in chat: spending money, legal, wiping player data, parties/guilds. Dom's direct word to a lane overrides a relayed plan. Waiting on Dom for anything else is a defect; flag it to Lead.
"""

BACKLOG_SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "archive",
    "archived",
    "legacy",
    "agent_archived",
    "agent_legacy",
}


def is_frankendom_cwd(cwd: Path) -> bool:
    """A Frankendom checkout (a lane folder or app worktree under a path naming it, or a clone of RPG-game). ~/.claude/hooks serves every
    project, so the delegation paragraph must not reach the others (Auditor, #141)."""
    if "frankendom" in str(cwd).lower():
        return True
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    return "rpg-game" in out.lower()


def delegation_for(cwd: Path) -> str:
    return DELEGATION_POLICY if is_frankendom_cwd(cwd) else ""


def additional_context(context: str) -> None:
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


def format_briefing(handoff: dict, mined: list[dict]) -> str:
    lines = [SESSION_BASELINE.rstrip(), "", "# Session Handoff"]
    if handoff.get("last_updated_at"):
        lines.append(f"Previous session updated: {handoff['last_updated_at']}")
    last_task = handoff.get("last_task") or {}
    if last_task:
        desc = last_task.get("description") or "Unnamed task"
        pr_url = last_task.get("pr_url") or ""
        lines.append(f"Last completed: {desc}" + (f" ({pr_url})" if pr_url else ""))
    backlog = handoff.get("queued_backlog") or mined
    if backlog:
        lines.append("Queued backlog:")
        for i, item in enumerate(backlog[:5], 1):
            lines.append(
                f"{i}. {item.get('description', 'Task')} "
                f"[{item.get('source_file', 'unknown')} · p{item.get('priority', 0)}]"
            )
        lines.append("Use the top item if the user has not provided a more specific task.")
    return "\n".join(lines)


def handoff_for_cwd(handoff: dict, cwd: Path) -> dict:
    if not handoff:
        return {}
    handoff_cwd = handoff.get("cwd")
    if handoff_cwd and Path(handoff_cwd).resolve() == cwd:
        return handoff
    # Legacy handoff files had no cwd and can leak unrelated backlog into a new
    # project session. Keep the baseline, drop the stale handoff.
    return {}


def filter_backlog(items: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for item in items or []:
        source = str(item.get("source_file") or "")
        parts = set(Path(source.split(":", 1)[0]).parts)
        if parts & BACKLOG_SKIP_PARTS:
            continue
        if any(part.startswith(".") for part in parts):
            continue
        if "Add coverage for Python function `test_" in str(item.get("description") or ""):
            continue
        filtered.append(item)
    return filtered


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = payload.get("session_id") or "unknown"
        cwd = Path(payload.get("cwd") or ".").resolve()
        prompt = (
            payload.get("prompt")
            or payload.get("initial_prompt")
            or payload.get("last_user_message")
            or latest_task_slice(payload.get("transcript_path") or "", "")
        )
        if prompt:
            write_session_spec(session_id, prompt)
            log(COMPONENT, f"session spec captured chars={len(prompt)}")
        remember_repo(cwd)
        handoff = handoff_for_cwd(load_handoff(), cwd)
        if handoff.get("queued_backlog"):
            original_count = len(handoff.get("queued_backlog") or [])
            handoff["queued_backlog"] = filter_backlog(handoff.get("queued_backlog") or [])
            if len(handoff["queued_backlog"]) != original_count:
                write_handoff(handoff)
        mined: list[dict] = []
        if not handoff.get("queued_backlog") and cwd.exists() and is_git_repo(cwd):
            mined = filter_backlog(mine_backlog(cwd, max_items=10))
            if mined and not handoff:
                handoff = {
                    "session_id": session_id,
                    "cwd": str(cwd),
                    "queued_backlog": mined,
                    "tasks_completed_count": 0,
                    "session_started_at": now_iso(),
                }
                write_handoff(handoff)
        if handoff or mined:
            context = format_briefing(handoff, mined)
            log(COMPONENT, f"context injected; backlog={len(handoff.get('queued_backlog') or mined)}")
            additional_context(context + ("\n" + delegation_for(cwd) if delegation_for(cwd) else ""))
        else:
            log(COMPONENT, "baseline context injected; no handoff/backlog")
            additional_context(SESSION_BASELINE + delegation_for(cwd))
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

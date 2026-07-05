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
from lib.claude_adapter import latest_task_slice_claude as latest_task_slice


COMPONENT = "session_start"

SESSION_BASELINE = """# V4/MCP Session Baseline
- Project/code/repo work: use knowledge MCP `get_playbook()` when available; read local `AGENTS.md` and `PROJECT_STATE.md` when present.
- Route tools deliberately: knowledge MCP for doctrine, GitHub for PR/CI, Browser for UI/local web, official docs/web for current facts.
- Mandatory: use Codegraph MCP first for structural code context/impact when available; if skipping, state why.
- Keep command output lean: targeted commands, capped output, summarized failures, narrow `rg` paths.
- No full diffs/logs/generated files/archives/dependency trees/repeated output unless needed.
- Avoid duplicate parallel exploration. Split scout, surgeon, reviewer with non-overlapping scope.
- Start project work from a compact context packet: task, known files, do-not-inspect paths, evidence, success checks.
- Scale V4 loop to task: success condition, likely files, smallest reversible change, strong verification.
- Prefer minimal LOC: delete/refactor before adding; avoid hardcoded or silo-specific fixes unless scoped.
- Before stopping, run self-review plus strongest relevant automated/judge check.
- Keep context on current task; avoid old-history trawling unless required.
- Default to autonomous execution. Do not ask for confirmation when a reasonable reversible default exists.
- Missing detail: choose conservative project-local default, state briefly, keep working.
- Ask only for true blockers: secrets, destructive/irreversible actions, live external side effects, purchases, regulated decisions, high-blast-radius product choices.
- Long runs: bounded reversible progress with logs/checkpoints over micro-management.
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


def additional_context(context: str) -> None:
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


def format_briefing(handoff: dict, mined: list[dict]) -> str:
    lines = [SESSION_BASELINE.rstrip(), "", "# Session Handoff"]
    codegraph = codegraph_notice(handoff.get("cwd"))
    if codegraph:
        lines.extend(["", codegraph])
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


def codegraph_notice(cwd: str | None) -> str:
    if not cwd:
        return ""
    path = Path(cwd).resolve()
    if not is_git_repo(path):
        return ""
    if (path / ".codegraph" / "codegraph.db").exists():
        return ""
    return "# Codegraph\n- No local index found for this repo; run `codegraph init -i` before structural code work."


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
            additional_context(context)
        else:
            log(COMPONENT, "baseline context injected; no handoff/backlog")
            codegraph = codegraph_notice(str(cwd))
            additional_context(SESSION_BASELINE if not codegraph else f"{SESSION_BASELINE.rstrip()}\n\n{codegraph}")
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

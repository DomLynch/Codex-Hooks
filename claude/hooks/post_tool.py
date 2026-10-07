#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.api import exit_if_audit_child
from lib.autonomy import hook_env, remember_repo
from lib.git import changed_files, commit, current_session_branch, ensure_branch, git_root, is_git_repo, push, resolve_git_cwd
from lib.log import log
from lib.quality import pending_quality, run_quality_checks
from lib.claude_adapter import changed_files_in_claude_post_tool as changed_files_in_payload
from lib.state import checkpoint_state, update_checkpoint_state


COMPONENT = "post_tool"
CHECKPOINT_EDIT_THRESHOLD = 5
CHECKPOINT_TIME_THRESHOLD_S = 1800
CHECKPOINT_BRANCH_RETRY_S = 1800
# Large repo test modules can legitimately exceed 60s when the quality floor
# reruns a whole changed file after patching. Match the stop hook's changed-test
# budget so post-tool checks fail on real failures, not timeout drift.
QUALITY_TIMEOUT_S = 120
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}  # Claude Code edit tools
# Checkpoints switch the checkout to claude/<session>/main, commit and push: the
# a side effect, so CODEX_HOOK_AUTO_PR (historic name; auto-PR itself was removed) governs it.
# Off unless explicitly "1": a missing or unreadable hook_env must not turn on commit+push.
CHECKPOINTS_ENABLED = hook_env("CODEX_HOOK_AUTO_PR", "0") == "1"


def block(reason: str) -> None:
    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        },
    }))
    sys.exit(0)


def seconds_since(iso: str) -> int:
    if not iso:
        return 10**9
    try:
        then = datetime.fromisoformat(iso)
        return int((datetime.now(timezone.utc) - then).total_seconds())
    except Exception:
        return 10**9


def format_quality_failures(result: dict) -> str:
    lines = ["[Quality Floor] Tool result introduced failing checks. Fix before continuing."]
    for item in result.get("results", []):
        if item.get("ok"):
            continue
        lines.append(f"\n{item.get('tool')} failed for {item.get('file')}:\n{item.get('output', '').strip()}")
    return "\n".join(lines)[:8000]


def maybe_checkpoint(session_id: str, cwd: Path) -> None:
    if not CHECKPOINTS_ENABLED:
        return
    if not is_git_repo(cwd):
        log(COMPONENT, "checkpoint skipped: not a git repo")
        return
    state = checkpoint_state(session_id)
    edits = int(state.get("edits_since_last_commit", 0))
    if seconds_since(state.get("last_branch_failure_at", "")) < CHECKPOINT_BRANCH_RETRY_S:
        return
    due_by_edits = edits >= CHECKPOINT_EDIT_THRESHOLD
    last_commit_at = state.get("last_commit_at", "")
    due_by_time = bool(last_commit_at) and seconds_since(last_commit_at) >= CHECKPOINT_TIME_THRESHOLD_S and edits > 0
    if not (due_by_edits or due_by_time):
        return
    branch = current_session_branch(session_id)
    if not ensure_branch(branch, cwd):
        update_checkpoint_state(session_id, branch_failed=True)
        log(COMPONENT, f"checkpoint skipped: could not ensure {branch}")
        return
    ok, sha = commit(f"[checkpoint] session {session_id[:8]} - {edits} edits", cwd, state.get("files", []))
    if not ok:
        log(COMPONENT, f"checkpoint skipped/no changes: {sha}")
        return
    update_checkpoint_state(session_id, committed=True)
    pushed = push(branch, cwd)
    log(COMPONENT, f"checkpoint commit {sha}; push={pushed}")


def main() -> None:
    exit_if_audit_child()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool_name = payload.get("tool_name") or ""
        session_id = payload.get("session_id") or "unknown"
        tool_input = payload.get("tool_input") or {}
        workdir = tool_input.get("workdir") if isinstance(tool_input, dict) else None
        cwd = Path(workdir or payload.get("cwd") or ".").resolve()
        is_edit = tool_name.rsplit("__", 1)[-1] in EDIT_TOOLS or "*** Begin Patch" in str(tool_input)
        files = changed_files_in_payload(payload) if is_edit else []
        targets = {}
        for file in files:
            root = git_root(file.parent)
            if root:
                targets.setdefault(root, []).append(file)
        for root in targets:
            pending_quality(session_id, root)
        cwd = resolve_git_cwd(cwd, files)
        remember_repo(cwd)
        if not files and is_git_repo(cwd):
            files = changed_files(cwd)
        if not targets and files:
            targets[cwd] = files
        deadline = time.monotonic() + QUALITY_TIMEOUT_S
        for root, target_files in targets.items():
            result = run_quality_checks(target_files, root, timeout=deadline - time.monotonic())
            log(COMPONENT, f"quality files={len(target_files)} ok={result.get('ok')} cwd={root}")
            if not result.get("ok"):
                block(format_quality_failures(result))
        if is_edit:
            update_checkpoint_state(session_id, edits=1, files=files)
            maybe_checkpoint(session_id, cwd)
    except Exception as exc:
        log(COMPONENT, f"quality error: {exc}")
        block(f"[Quality Floor] Unable to verify edited files: {exc}")


if __name__ == "__main__":
    main()

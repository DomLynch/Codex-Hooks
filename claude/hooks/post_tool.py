#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.api import call_mimo, load_api_key
from lib.git import changed_files, commit, current_session_branch, ensure_branch, is_git_repo, push, resolve_git_cwd
from lib.json_state import load_json_locked, update_json_locked
from lib.log import log
from lib.quality import changed_files_in_payload as _codex_changed_files, run_quality_checks
from lib.claude_adapter import changed_files_in_claude_post_tool as changed_files_in_payload, latest_task_slice_claude
from lib.state import checkpoint_state, now_iso, update_checkpoint_state
from lib.claude_adapter import latest_task_slice_claude as latest_task_slice


COMPONENT = "post_tool"
CHECKPOINT_EDIT_THRESHOLD = 5
CHECKPOINT_TIME_THRESHOLD_S = 1800
QUALITY_TIMEOUT_S = 60
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}  # Claude Code edit tools
MIMO_CHECKPOINT_AUDIT_PATH = Path.home() / ".claude" / "state" / "post_tool_mimo_audit.json"
MIMO_AUDIT_EDIT_THRESHOLD = 5
MIMO_AUDIT_TIME_THRESHOLD_S = 1800
MIMO_AUDIT_MIN_TRANSCRIPT_CHARS = 400
MIMO_AUDIT_MAX_TRANSCRIPT_CHARS = 3000
MIMO_AUDIT_TIMEOUT_S = 30

MIMO_CHECKPOINT_PROMPT = """Audit Claude Code's in-progress task checkpoint.
Your first line must be exactly one of:
VERDICT: PASS
VERDICT: ISSUES

Check only the current task slice. Block only for concrete problems:
- Claude Code is drifting from the user's request or asking for unnecessary confirmation instead of using a reasonable reversible default
- The latest edits appear incorrect, unsafe, incomplete, or outside scope
- Required checks/logs are missing for the risk level
- V4/MCP/project guidance is being ignored on project/code work

If there is no concrete blocker, return:
VERDICT: PASS

If there are blockers, return:
VERDICT: ISSUES
1. <specific fix Claude Code should make now>

Current task slice:
---
{transcript}
---
"""


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


def parse_mimo_verdict(text: str) -> tuple[str, str]:
    stripped = (text or "").strip()
    if stripped.startswith("VERDICT: PASS"):
        return "PASS", stripped
    if stripped.startswith("VERDICT: ISSUES"):
        return "ISSUES", stripped
    for marker in ("VERDICT: PASS", "VERDICT: ISSUES"):
        idx = stripped.find(marker)
        if idx >= 0:
            normalized = stripped[idx:].strip()
            return ("PASS" if marker.endswith("PASS") else "ISSUES"), normalized
    return "UNPARSEABLE", stripped


def audit_state(session_id: str) -> dict:
    data = load_json_locked(MIMO_CHECKPOINT_AUDIT_PATH, {})
    state = data.get(session_id) if isinstance(data, dict) else {}
    if not isinstance(state, dict):
        state = {}
    return {
        "edits_since_last_audit": int(state.get("edits_since_last_audit", 0)),
        "last_audit_at": state.get("last_audit_at", ""),
        "last_verdict": state.get("last_verdict", ""),
        "last_preview": state.get("last_preview", ""),
    }


def update_audit_state(session_id: str, edits: int = 0, audited: bool = False, verdict: str = "", preview: str = "") -> dict:
    now = now_iso()

    def updater(data: dict) -> dict:
        state = data.get(session_id)
        if not isinstance(state, dict):
            state = {"edits_since_last_audit": 0, "last_audit_at": now}
        if edits:
            state["edits_since_last_audit"] = max(0, int(state.get("edits_since_last_audit", 0)) + edits)
            state.setdefault("last_audit_at", now)
        if audited:
            state["edits_since_last_audit"] = 0
            state["last_audit_at"] = now
            state["last_verdict"] = verdict
            state["last_preview"] = preview[:500]
        state["last_updated_at"] = now
        data[session_id] = state
        if len(data) > 50:
            data = dict(list(data.items())[-50:])
        return data

    return update_json_locked(MIMO_CHECKPOINT_AUDIT_PATH, updater).get(session_id, {})


def mimo_checkpoint_due(state: dict) -> bool:
    edits = int(state.get("edits_since_last_audit", 0))
    due_by_edits = edits >= MIMO_AUDIT_EDIT_THRESHOLD
    last_audit_at = state.get("last_audit_at", "")
    due_by_time = edits > 0 and bool(last_audit_at) and seconds_since(last_audit_at) >= MIMO_AUDIT_TIME_THRESHOLD_S
    return due_by_edits or due_by_time


def maybe_mimo_checkpoint_audit(payload: dict, session_id: str, cwd: Path) -> None:
    state = audit_state(session_id)
    if not mimo_checkpoint_due(state):
        return

    transcript = latest_task_slice(
        payload.get("transcript_path") or "",
        payload.get("last_assistant_message") or "",
        max_chars=MIMO_AUDIT_MAX_TRANSCRIPT_CHARS,
    )
    if len(transcript) < MIMO_AUDIT_MIN_TRANSCRIPT_CHARS:
        update_audit_state(session_id, audited=True, verdict="SKIP_TOO_SHORT", preview=f"{len(transcript)} chars")
        log(COMPONENT, f"mimo checkpoint skipped: transcript too short ({len(transcript)} chars)")
        return
    if not load_api_key("MIMO_API_KEY", "mimo_api_key"):
        update_audit_state(session_id, audited=True, verdict="SKIP_NO_KEY", preview="MIMO_API_KEY missing")
        log(COMPONENT, "mimo checkpoint skipped: MIMO_API_KEY missing")
        return

    prompt = MIMO_CHECKPOINT_PROMPT.format(transcript=transcript)
    try:
        response = call_mimo(prompt, max_tokens=1000, timeout=MIMO_AUDIT_TIMEOUT_S)
    except Exception as exc:
        update_audit_state(session_id, audited=True, verdict="ERROR", preview=str(exc))
        log(COMPONENT, f"mimo checkpoint fail-open: {exc}")
        return

    verdict, normalized = parse_mimo_verdict(response)
    update_audit_state(session_id, audited=True, verdict=verdict, preview=normalized)
    log(COMPONENT, f"mimo checkpoint verdict={verdict} transcript={len(transcript)}")
    if verdict == "ISSUES":
        block(
            "[MiMo checkpoint audit]\n\n"
            "MiMo-v2.5-Pro reviewed this in-progress task checkpoint and found concrete issues. "
            "Fix these before continuing:\n\n"
            f"{normalized[:5000]}"
        )
    if verdict == "UNPARSEABLE":
        log(COMPONENT, f"mimo checkpoint unparseable; allowing progress preview={normalized[:180]}")


def maybe_checkpoint(session_id: str, cwd: Path) -> None:
    if not is_git_repo(cwd):
        log(COMPONENT, "checkpoint skipped: not a git repo")
        return
    state = checkpoint_state(session_id)
    edits = int(state.get("edits_since_last_commit", 0))
    due_by_edits = edits >= CHECKPOINT_EDIT_THRESHOLD
    last_commit_at = state.get("last_commit_at", "")
    due_by_time = bool(last_commit_at) and seconds_since(last_commit_at) >= CHECKPOINT_TIME_THRESHOLD_S and edits > 0
    if not (due_by_edits or due_by_time):
        return
    branch = current_session_branch(session_id)
    if not ensure_branch(branch, cwd):
        log(COMPONENT, f"checkpoint skipped: could not ensure {branch}")
        return
    ok, sha = commit(f"[checkpoint] session {session_id[:8]} - {edits} edits", cwd)
    if not ok:
        log(COMPONENT, f"checkpoint skipped/no changes: {sha}")
        return
    update_checkpoint_state(session_id, committed=True)
    pushed = push(branch, cwd)
    log(COMPONENT, f"checkpoint commit {sha}; push={pushed}")


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool_name = payload.get("tool_name") or ""
        session_id = payload.get("session_id") or "unknown"
        cwd = Path(payload.get("cwd") or ".").resolve()
        files = changed_files_in_payload(payload)
        cwd = resolve_git_cwd(cwd, files)
        if not files and is_git_repo(cwd):
            files = changed_files(cwd)[:10]
        if files:
            result = run_quality_checks(files, cwd, timeout=QUALITY_TIMEOUT_S)
            log(COMPONENT, f"quality files={len(files)} ok={result.get('ok')}")
            if not result.get("ok"):
                block(format_quality_failures(result))
        if tool_name in EDIT_TOOLS or files:
            update_checkpoint_state(session_id, edits=1)
            update_audit_state(session_id, edits=1)
            maybe_mimo_checkpoint_audit(payload, session_id, cwd)
            maybe_checkpoint(session_id, cwd)
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

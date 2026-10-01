#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.api import call_mimo
from lib.autonomy import record_lesson, remember_repo
from lib.git import changed_files, git_root, is_git_repo, resolve_git_cwd
from lib.json_state import load_json_locked, update_json_locked
from lib.log import log
from lib.quality import changed_files_in_payload, pending_quality, run_quality_checks
from lib.state import now_iso
from lib.transcript import latest_task_slice


COMPONENT = "post_tool"
# Large repo test modules can legitimately exceed 60s when the quality floor
# reruns a whole changed file after patching. Match the stop hook's changed-test
# budget so post-tool checks fail on real failures, not timeout drift.
QUALITY_TIMEOUT_S = 120
EDIT_TOOLS = {"apply_patch", "Edit", "Write", "MultiEdit"}
MIMO_CHECKPOINT_AUDIT_PATH = Path.home() / ".codex" / "state" / "post_tool_mimo_audit.json"
MIMO_AUDIT_EDIT_THRESHOLD = 15
MIMO_AUDIT_TIME_THRESHOLD_S = 1800
MIMO_AUDIT_MIN_TRANSCRIPT_CHARS = 400
MIMO_AUDIT_MAX_TRANSCRIPT_CHARS = 3000
MIMO_AUDIT_TIMEOUT_S = 30

MIMO_CHECKPOINT_PROMPT = """Audit Codex's in-progress task checkpoint.
Your first line must be exactly one of:
VERDICT: PASS
VERDICT: ISSUES

Check only the current task slice. Block only for concrete problems:
- Codex is drifting from the user's request or asking for unnecessary confirmation instead of using a reasonable reversible default
- The latest edits already show a concrete correctness, security, or scope regression
- Repeated command or test failures are being ignored instead of diagnosed
- V4/MCP/project guidance is being ignored on non-trivial code work: no three-query Semble discovery, skipped Codegraph structural/impact analysis, or no stated reason

Equivalent wording is sufficient; cosmetic/acknowledgement phrasing is nonblocking.
Check current scope, already-completed work, and the SAME resource/revision before
flagging. Audit/status/pause requests do not authorize implementation. USER UPDATE
messages steer the original request; later explicit changes override conflicts.

This slice may omit earlier tool calls. Missing Semble/Codegraph receipts alone
are not evidence of skipped discovery; flag only explicit, current noncompliance.
Do not repeat an acknowledged past process mistake that cannot be redone.

This is an in-progress checkpoint. Do NOT block merely because implementation,
tests, commit, or final verification are unfinished; the Stop audit checks completion.
An acknowledged failure under active diagnosis is not an ignored failure. Do not
demand final tests/deployment at this checkpoint unless completion is claimed.
Still block a concrete unsafe edit or an unsupported claim that work is complete.
Match failures to the exact resource and expected outcome: negative tests are not
regressions. Proposed wording is not a claim about existing behavior.

If there is no concrete blocker, return:
VERDICT: PASS

If there are blockers, return:
VERDICT: ISSUES
1. <specific fix Codex should make now>

Current task slice:
---
{transcript}
---
"""


def sync_codegraph(root: Path) -> None:
    root = root.resolve()
    if git_root(root) != root:
        return
    indexed = (root / ".codegraph" / "codegraph.db").exists()
    try:
        command = ["codegraph", "sync", "-q"] if indexed else ["codegraph", "init"]
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=10 if indexed else 20)
        if result.returncode:
            log(COMPONENT, f"codegraph update failed cwd={root}: {result.stderr[-200:]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(COMPONENT, f"codegraph update unavailable cwd={root}: {exc}")


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
        preserve_request=True,
    )
    if len(transcript) < MIMO_AUDIT_MIN_TRANSCRIPT_CHARS:
        update_audit_state(session_id, audited=True, verdict="SKIP_TOO_SHORT", preview=f"{len(transcript)} chars")
        log(COMPONENT, f"mimo checkpoint skipped: transcript too short ({len(transcript)} chars)")
        return
    prompt = MIMO_CHECKPOINT_PROMPT.format(transcript=transcript)
    try:
        response = call_mimo(prompt, max_tokens=1000, timeout=MIMO_AUDIT_TIMEOUT_S, lane="checkpoint")
    except Exception as exc:
        update_audit_state(session_id, audited=True, verdict="ERROR", preview=str(exc))
        log(COMPONENT, f"mimo checkpoint fail-open: {exc}")
        return

    verdict, normalized = parse_mimo_verdict(response)
    update_audit_state(session_id, audited=True, verdict=verdict, preview=normalized)
    log(COMPONENT, f"mimo checkpoint verdict={verdict} transcript={len(transcript)}")
    if verdict == "ISSUES":
        record_lesson("mimo_checkpoint", normalized.splitlines()[1] if len(normalized.splitlines()) > 1 else normalized, cwd)
        block(
            "[GPT-6-Sol checkpoint audit]\n\n"
            "A fresh GPT-6-Sol session reviewed this in-progress task checkpoint and found concrete issues. "
            "Fix these before continuing:\n\n"
            f"{normalized[:5000]}"
        )
    if verdict == "UNPARSEABLE":
        log(COMPONENT, f"mimo checkpoint unparseable; allowing progress preview={normalized[:180]}")


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool_name = payload.get("tool_name") or ""
        session_id = payload.get("session_id") or "unknown"
        tool_input = payload.get("tool_input") or {}
        workdir = tool_input.get("workdir") if isinstance(tool_input, dict) else None
        cwd = Path(workdir or payload.get("cwd") or ".").resolve()
        is_edit = tool_name.rsplit("__", 1)[-1] in EDIT_TOOLS or "*** Begin Patch" in str(tool_input)
        files = changed_files_in_payload(payload) if is_edit else []
        autofix = bool(files)  # Never rewrite unrelated dirt discovered by the fallback.
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
            result = run_quality_checks(target_files, root, timeout=deadline - time.monotonic(), autofix=autofix)
            if is_edit:
                sync_codegraph(root)
            log(COMPONENT, f"quality files={len(target_files)} ok={result.get('ok')} cwd={root}")
            if not result.get("ok"):
                block(format_quality_failures(result))
        if is_edit:
            update_audit_state(session_id, edits=1)
            maybe_mimo_checkpoint_audit(payload, session_id, cwd)
    except Exception as exc:
        log(COMPONENT, f"quality error: {exc}")
        block(f"[Quality Floor] Unable to verify edited files: {exc}")


if __name__ == "__main__":
    main()

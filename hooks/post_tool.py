#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import remember_repo
from lib.git import changed_files, git_root, is_git_repo, resolve_git_cwd
from lib.log import log
from lib.quality import changed_files_in_payload, pending_quality, run_quality_checks


COMPONENT = "post_tool"
# Large repo test modules can legitimately exceed 60s when the quality floor
# reruns a whole changed file after patching. Match the stop hook's changed-test
# budget so post-tool checks fail on real failures, not timeout drift.
QUALITY_TIMEOUT_S = 120
EDIT_TOOLS = {"apply_patch", "Edit", "Write", "MultiEdit"}


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


def format_quality_failures(result: dict) -> str:
    lines = ["[Quality Floor] Tool result introduced failing checks. Fix before continuing."]
    for item in result.get("results", []):
        if item.get("ok"):
            continue
        lines.append(f"\n{item.get('tool')} failed for {item.get('file')}:\n{item.get('output', '').strip()}")
    return "\n".join(lines)[:8000]


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool_name = payload.get("tool_name") or ""
        session_id = payload.get("session_id") or "unknown"
        tool_input = payload.get("tool_input") or {}
        workdir = tool_input.get("workdir") if isinstance(tool_input, dict) else None
        cwd = Path(workdir or payload.get("cwd") or ".").resolve()
        is_edit = tool_name.rsplit("__", 1)[-1] in EDIT_TOOLS or "*** Begin Patch" in str(tool_input)
        if not is_edit:
            return
        files = changed_files_in_payload(payload)
        autofix = bool(files)  # Never rewrite unrelated dirt discovered by the fallback.
        targets = {}
        for file in files:
            root = git_root(file.parent)
            if root:
                targets.setdefault(root, []).append(file)
        cwd = resolve_git_cwd(cwd, files)
        remember_repo(cwd)
        if not files and is_git_repo(cwd):
            files = changed_files(cwd)
        if not targets and files:
            targets[cwd] = files
        for root in targets:
            pending_quality(session_id, root)
        deadline = time.monotonic() + QUALITY_TIMEOUT_S
        for root, target_files in targets.items():
            result = run_quality_checks(target_files, root, timeout=deadline - time.monotonic(), autofix=autofix)
            sync_codegraph(root)
            log(COMPONENT, f"quality files={len(target_files)} ok={result.get('ok')} cwd={root}")
            if not result.get("ok"):
                block(format_quality_failures(result))
    except Exception as exc:
        log(COMPONENT, f"quality error: {exc}")
        block(f"[Quality Floor] Unable to verify edited files: {exc}")


if __name__ == "__main__":
    main()

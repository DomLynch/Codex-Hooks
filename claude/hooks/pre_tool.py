#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import hook_env
from lib.log import log


COMPONENT = "pre_tool"
EDIT_TOOLS = {"apply_patch", "Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "Shell", "exec", "exec_command", "functions.exec_command"}
PROTECTED_PATH_PARTS = (
    "/.codex/secrets/",
    "/.claude/secrets/",
    "/.ssh/",
)
PROTECTED_FILENAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
AUDIT_ONLY_RE = re.compile(
    r"(audit|review|inspect|check).{0,120}(no code changes|do not edit|don't edit|read-only|audit only|review only)"
    r"|(no code changes|do not edit|don't edit|read-only|audit only|review only).{0,120}(audit|review|inspect|check)",
    re.IGNORECASE | re.DOTALL,
)

RISKY_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\s+push\b[^\n;&|]*(--force|-f)\b", re.IGNORECASE), "force-push is high blast radius"),
    (re.compile(r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-[^\s]*[fdx]", re.IGNORECASE), "destructive git cleanup"),
    (re.compile(r"\bgh\s+pr\s+merge\b|\bgh\s+repo\s+delete\b", re.IGNORECASE), "externally visible GitHub side effect"),
    (re.compile(r"\b(?:sudo\s+)?(?:rm|trash)\s+-[^\n;&|]*[rf][^\n;&|]*(?:\s+/|\s+~|\s+\$HOME|\s+/Users\b|\s+/opt\b|\s+/var\b|\s+/etc\b|\s+\*)", re.IGNORECASE), "broad destructive remove"),
    (re.compile(r"\b(?:sudo\s+)?(?:chmod|chown)\s+-R\b[^\n;&|]*(?:\s+/|\s+~|\s+\$HOME|\s+/Users\b|\s+/opt\b|\s+/var\b|\s+/etc\b)", re.IGNORECASE), "broad permission change"),
    (re.compile(r"\b(?:dd|mkfs|diskutil\s+erase|diskutil\s+partitionDisk)\b", re.IGNORECASE), "disk destructive command"),
    (re.compile(r"\b(drop\s+(database|table|schema)|truncate\s+(table\s+)?)\b", re.IGNORECASE), "destructive database command"),
    (re.compile(r"\b(terraform|tofu)\s+(apply|destroy)\b|\bkubectl\s+(apply|delete|scale)\b", re.IGNORECASE), "infra/prod side effect"),
    (re.compile(r"\b(aws|gcloud|az)\b[^\n;&|]*\b(delete|terminate|destroy|remove)\b", re.IGNORECASE), "cloud destructive command"),
    (re.compile(r"\bcurl\b[^\n;&|]*\|\s*(sh|bash)\b", re.IGNORECASE), "remote script execution"),
    (re.compile(r"\b(stripe|payment|billing|purchase)\b", re.IGNORECASE), "payment/billing side effect"),
    (re.compile(r"\b(ssh|scp|rsync)\b[^\n;&|]*\b(sudo|systemctl|service|restart|reload|deploy|migrate|rm\s+-|git\s+pull|git\s+reset)\b", re.IGNORECASE), "remote live side effect"),
)


def block(reason: str) -> None:
    message = (
        "[PreToolUse blast fence]\n\n"
        f"{reason}\n\n"
        "This hook only blocks high-blast-radius or explicit read-only violations. "
        "Use an explicit user-approved path or disable the hook env flag if this is intentional."
    )
    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": message,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        },
    }))
    sys.exit(0)


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def latest_user_prompt(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    latest = ""
    try:
        for line in p.read_text(errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            payload = obj.get("payload") or {}
            if obj.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                text = content_text(payload.get("content"))
            elif obj.get("type") == "user":
                message = obj.get("message") or {}
                text = content_text(message.get("content"))
            else:
                continue
            if text and not text.lstrip().startswith("<hook_prompt"):
                latest = text
    except Exception:
        return ""
    return latest


def tool_name(payload: dict) -> str:
    return str(payload.get("tool_name") or payload.get("toolName") or payload.get("name") or "")


def tool_input(payload: dict) -> dict:
    value = payload.get("tool_input") or payload.get("toolInput") or payload.get("input") or {}
    return value if isinstance(value, dict) else {}


def shell_command(data: dict) -> str:
    return str(data.get("command") or data.get("cmd") or data.get("script") or "")


def candidate_paths(data: dict) -> list[Path]:
    paths: list[Path] = []
    for key in ("file_path", "path", "notebook_path"):
        value = data.get(key)
        if value:
            paths.append(Path(str(value)).expanduser())
    for item in data.get("edits") or []:
        if isinstance(item, dict) and item.get("file_path"):
            paths.append(Path(str(item["file_path"])).expanduser())
    return paths


def protected_path(path: Path) -> bool:
    raw = str(path)
    try:
        raw = str(path.resolve())
    except Exception:
        pass
    if any(part in raw for part in PROTECTED_PATH_PARTS):
        return True
    return path.name in PROTECTED_FILENAMES or path.suffix in {".pem", ".key", ".p12"}


def risky_command_reason(command: str) -> str:
    normalized = " ".join(command.split())
    for pattern, reason in RISKY_COMMANDS:
        if pattern.search(normalized):
            return reason
    return ""


def main() -> None:
    try:
        if hook_env("CODEX_HOOK_PRETOOL_FENCE", "1") == "0" or os.environ.get("CODEX_HOOK_PRETOOL_FENCE") == "0":
            return
        payload = json.loads(sys.stdin.read() or "{}")
        name = tool_name(payload)
        data = tool_input(payload)
        latest_prompt = latest_user_prompt(str(payload.get("transcript_path") or ""))
        edit_like = name in EDIT_TOOLS or name.lower() in {item.lower() for item in EDIT_TOOLS}
        if edit_like and AUDIT_ONLY_RE.search(latest_prompt):
            block("latest user request is audit/review-only with no code changes, but an edit tool is about to run")
        if edit_like:
            for path in candidate_paths(data):
                if protected_path(path):
                    block(f"edit targets protected secret/config path: {path}")
        if name in SHELL_TOOLS or name.lower() in {item.lower() for item in SHELL_TOOLS}:
            command = shell_command(data)
            reason = risky_command_reason(command)
            if reason:
                block(f"{reason}: {command[:500]}")
        log(COMPONENT, f"allow tool={name}")
    except SystemExit:
        raise
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

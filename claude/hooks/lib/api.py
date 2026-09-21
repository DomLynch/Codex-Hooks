from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .autonomy import hook_env
from .log import log


CLAUDE_SECRETS_DIR = Path.home() / ".claude" / "secrets"
CODEX_SECRETS_DIR = Path.home() / ".codex" / "secrets"  # Fallback for shared audit keys
CODEX_AUDIT_MODEL = os.environ.get("CODEX_AUDIT_MODEL", "gpt-5.6-terra")
CODEX_AUDIT_EFFORT = os.environ.get("CODEX_AUDIT_EFFORT", "high")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
# Reviewer model and effort: the environment, then ~/.claude/secrets/hook_env (where the owner sets them), then the default.
CLAUDE_AUDIT_MODEL = hook_env("CLAUDE_AUDIT_MODEL", "claude-sonnet-5").strip()
CLAUDE_AUDIT_EFFORT = hook_env("CLAUDE_AUDIT_EFFORT", "high").strip().lower()
# "claude" = Claude CLI on the user's subscription; "codex" = GPT via Codex CLI.
AUDIT_TRANSPORT = hook_env("CLAUDE_AUDIT_TRANSPORT", "claude").strip().lower()
# Set on the audit child's environment so hooks inside it exit immediately (no recursion).
AUDIT_CHILD_ENV = "CLAUDE_HOOK_AUDIT_CHILD"
AUDITOR_NAME = f"Claude {CLAUDE_AUDIT_MODEL}" if AUDIT_TRANSPORT == "claude" else CODEX_AUDIT_MODEL


def exit_if_audit_child() -> None:
    """Hooks call this first: a Claude audit child must never run hooks, or it recurses."""
    if os.environ.get(AUDIT_CHILD_ENV):
        raise SystemExit(0)


def anthropic_text(data: dict) -> str:
    parts = data.get("content") or []
    return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()


def load_api_key(env_name: str, secrets_filename: str) -> str:
    """Load an API key. Order: env var → ~/.claude/secrets → ~/.codex/secrets → shell rc parse.
    The ~/.codex fallback lets Anthropic keys be shared across both hook systems.
    """
    key = os.environ.get(env_name)
    if key:
        return key.strip()
    for base in (CLAUDE_SECRETS_DIR, CODEX_SECRETS_DIR):
        path = base / secrets_filename
        if path.exists():
            try:
                return path.read_text().strip()
            except Exception:
                pass
    for rc_file in (".zshrc", ".zprofile", ".profile"):
        p = Path.home() / rc_file
        if not p.exists():
            continue
        try:
            lines = p.read_text(errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("export "):
                stripped = stripped[len("export "):].strip()
            if not stripped.startswith(f"{env_name}="):
                continue
            raw = stripped.split("=", 1)[1].strip()
            try:
                parts = shlex.split(raw, comments=False, posix=True)
            except ValueError:
                parts = [raw.strip("\"'")]
            return parts[0] if parts else ""
    return ""


def codex_binary() -> str:
    candidates = (
        os.environ.get("CODEX_AUDIT_BIN"),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        shutil.which("codex"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Codex CLI is unavailable; set CODEX_AUDIT_BIN")


def claude_binary() -> str:
    # GUI-launched hooks don't inherit the shell PATH; list the usual absolute paths.
    candidates = (
        os.environ.get("CLAUDE_AUDIT_BIN"),
        shutil.which("claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        str(Path.home() / ".local" / "bin" / "claude"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Claude CLI is unavailable; set CLAUDE_AUDIT_BIN")


def call_claude_cli(
    prompt: str,
    max_tokens: int = 1500,
    timeout: int = 45,
    lane: str = "shared",
    system: str = "",
) -> str:
    """Audit via the Claude CLI on the user's subscription (no API key).

    The child inherits AUDIT_CHILD_ENV so every hook it would fire exits at once.
    Mutating tools are disallowed: the auditor only reads the prompt and replies.
    """
    del max_tokens
    instruction = f"{system}\n\n{prompt}" if system else prompt
    started = time.monotonic()
    env = {**os.environ, AUDIT_CHILD_ENV: "1"}
    # `claude setup-token` prints a long-lived token; it lives in hook_env, not the keychain.
    token = hook_env("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    command = [
        claude_binary(), "-p",
        "--model", CLAUDE_AUDIT_MODEL,
        "--effort", CLAUDE_AUDIT_EFFORT,
        "--disallowedTools", "Bash Edit Write NotebookEdit",
    ]
    with tempfile.TemporaryDirectory(prefix="claude-audit-") as workdir:
        try:
            completed = subprocess.run(
                command, input=instruction, text=True, capture_output=True,
                cwd=workdir, env=env, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Claude audit timed out after {timeout}s") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise RuntimeError(f"Claude audit failed ({completed.returncode}): {detail}")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log("claude_audit", f"CLAUDE_AUDIT lane={lane} model={CLAUDE_AUDIT_MODEL} effort={CLAUDE_AUDIT_EFFORT} elapsed_ms={elapsed_ms}")
    return completed.stdout.strip()


def call_mimo(
    prompt: str,
    max_tokens: int = 1500,
    timeout: int = 45,
    lane: str = "shared",
    system: str = "",
    api_key: str = "",
) -> str:
    if AUDIT_TRANSPORT == "claude":
        return call_claude_cli(prompt, max_tokens=max_tokens, timeout=timeout, lane=lane, system=system)
    del api_key, max_tokens
    instruction = f"{system}\n\n{prompt}" if system else prompt
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codex-audit-") as workdir:
        output_path = Path(workdir) / "verdict.txt"
        command = [
            codex_binary(), "exec", "--ignore-user-config", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only",
            "--model", CODEX_AUDIT_MODEL,
            "--config", f'model_reasoning_effort="{CODEX_AUDIT_EFFORT}"',
            "--output-last-message", str(output_path), "-",
        ]
        try:
            completed = subprocess.run(
                command, input=instruction, text=True, capture_output=True,
                cwd=workdir, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Codex audit timed out after {timeout}s") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise RuntimeError(f"Codex audit failed ({completed.returncode}): {detail}")
        response = output_path.read_text(errors="replace").strip() if output_path.exists() else completed.stdout.strip()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log("codex_audit", f"CODEX_AUDIT lane={lane} model={CODEX_AUDIT_MODEL} effort={CODEX_AUDIT_EFFORT} elapsed_ms={elapsed_ms}")
    return response


def call_claude(prompt: str, system: str = "", max_tokens: int = 1500, timeout: int = 45) -> str:
    key = load_api_key("ANTHROPIC_API_KEY", "anthropic_api_key")
    if not key:
        return ""
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return anthropic_text(data)

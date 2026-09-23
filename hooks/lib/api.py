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

from .log import log


SECRETS_DIR = Path.home() / ".codex" / "secrets"
CODEX_AUDIT_MODEL = os.environ.get("CODEX_AUDIT_MODEL", "gpt-6-sol")
CODEX_AUDIT_EFFORT = os.environ.get("CODEX_AUDIT_EFFORT", "low")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")


def anthropic_text(data: dict) -> str:
    parts = data.get("content") or []
    return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()


def load_api_key(env_name: str, secrets_filename: str) -> str:
    key = os.environ.get(env_name)
    if key:
        return key.strip()
    path = SECRETS_DIR / secrets_filename
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


def call_mimo(
    prompt: str,
    max_tokens: int = 1500,
    timeout: int = 45,
    lane: str = "shared",
    system: str = "",
    api_key: str = "",
) -> str:
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

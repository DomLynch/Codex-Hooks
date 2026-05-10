from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


SECRETS_DIR = Path.home() / ".codex" / "secrets"
MIMO_BASE_URL = "https://token-plan-sgp.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5-pro"
OPENCLAW_MIMO_MODEL = os.environ.get("CODEX_HOOK_OPENCLAW_MIMO_MODEL", "mimio/mimo-v2.5-pro")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")


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


def call_mimo_via_openclaw(prompt: str, max_tokens: int = 1500, timeout: int = 45) -> str:
    if os.environ.get("CODEX_HOOK_MIMO_VIA_OPENCLAW", "1") == "0":
        return ""
    binary = shutil.which("openclaw")
    if not binary:
        return ""
    try:
        proc = subprocess.run(
            [
                binary,
                "infer",
                "model",
                "run",
                *(["--gateway"] if os.environ.get("CODEX_HOOK_OPENCLAW_GATEWAY") == "1" else []),
                "--model",
                OPENCLAW_MIMO_MODEL,
                "--prompt",
                prompt,
                "--json",
            ],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""
    outputs = data.get("outputs") or []
    if not data.get("ok") or not outputs:
        return ""
    return str(outputs[0].get("text") or "").strip()


def call_mimo(prompt: str, max_tokens: int = 1500, timeout: int = 45) -> str:
    via_openclaw = call_mimo_via_openclaw(prompt, max_tokens=max_tokens, timeout=timeout)
    if via_openclaw:
        return via_openclaw
    key = load_api_key("MIMO_API_KEY", "mimo_api_key")
    if not key:
        return ""
    body = json.dumps({
        "model": MIMO_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{MIMO_BASE_URL}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or msg.get("reasoning_content") or "").strip()


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
    parts = data.get("content") or []
    return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()

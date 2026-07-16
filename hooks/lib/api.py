from __future__ import annotations

import json
import os
import shlex
import time
import urllib.request
from pathlib import Path

from .log import log


SECRETS_DIR = Path.home() / ".codex" / "secrets"
MIMO_BASE_URL = "https://api.minimax.io/anthropic"
MIMO_MODEL = "MiniMax-M3"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")


def anthropic_text(data: dict) -> str:
    parts = data.get("content") or []
    return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()


def minimax_usage_message(data: dict, lane: str, elapsed_ms: int) -> str:
    usage = data.get("usage") or {}
    return (
        f"MINIMAX_USAGE lane={lane} model={data.get('model') or MIMO_MODEL} "
        f"input_tokens={int(usage.get('input_tokens') or 0)} "
        f"output_tokens={int(usage.get('output_tokens') or 0)} "
        f"cache_read_tokens={int(usage.get('cache_read_input_tokens') or 0)} "
        f"cache_write_tokens={int(usage.get('cache_creation_input_tokens') or 0)} "
        f"elapsed_ms={elapsed_ms}"
    )


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


def call_mimo(prompt: str, max_tokens: int = 1500, timeout: int = 45, lane: str = "shared") -> str:
    key = load_api_key("MINIMAX_API_KEY", "minimax_api_key")
    if not key:
        return ""
    body = json.dumps({
        "model": MIMO_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{MIMO_BASE_URL}/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    log("minimax_api", minimax_usage_message(data, lane, int((time.monotonic() - started) * 1000)))
    return anthropic_text(data)


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

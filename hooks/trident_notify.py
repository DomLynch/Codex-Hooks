#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


COMPONENT = "trident_notify"
LOG_DIR = Path.home() / ".codex" / "logs"
MIMO_VERDICT_PATH = Path.home() / ".codex" / "state" / "mimo_last_verdict.json"
OUTGOING_LOG_PATH = LOG_DIR / "trident_notify_outgoing.log"


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"{COMPONENT}.log", "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def compact(text: str, limit: int = 3500) -> str:
    text = re.sub(r"tp-[A-Za-z0-9]+", "tp-<redacted>", text or "")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    text = re.sub(r"ghp_[A-Za-z0-9_]+", "ghp_<redacted>", text)
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[truncated]...\n" + text[-limit // 2 :]


def short(value: object, fallback: str) -> str:
    text = str(value or fallback)
    return text[:12]


def read_mimo_verdict(session_id: str, turn_id: str, timeout_s: int = 25) -> tuple[str, str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            data = json.loads(MIMO_VERDICT_PATH.read_text())
        except Exception:
            data = {}
        state = data.get(session_id) or {}
        if state and (not turn_id or state.get("turn_id") == turn_id):
            return str(state.get("verdict") or "UNKNOWN"), compact(str(state.get("raw") or state.get("normalized") or ""), 800)
        time.sleep(1)
    log(f"mimo verdict not available within {timeout_s}s; sending without")
    return "NONE", ""


def build_prompt(payload: dict, verdict: str, verdict_preview: str) -> str:
    session_id = payload.get("session_id") or "unknown"
    turn_id = payload.get("turn_id") or ""
    cwd = payload.get("cwd") or ""
    assistant = compact(str(payload.get("last_assistant_message") or ""))
    return (
        "[CODEX stop notification - Trident push hook]\n"
        "agent: codex\n"
        f"session_id: {session_id}\n"
        f"turn_id: {turn_id}\n"
        f"cwd: {cwd}\n"
        f"mimo_verdict: {verdict}\n"
        f"mimo_preview: {verdict_preview}\n\n"
        "--- last assistant message ---\n"
        f"{assistant}\n"
        "--- end ---\n\n"
        "Decide: queue follow-up work, ping the user, chain next task, or just log."
    )


def spawn_openclaw(payload: dict, prompt: str) -> tuple[bool, str]:
    openclaw = shutil.which("openclaw")
    if not openclaw:
        return False, "openclaw binary not on PATH"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    session_id = short(payload.get("session_id"), "unknown")
    turn_id = short(payload.get("turn_id"), str(int(time.time())))
    agent_id = os.environ.get("CODEX_HOOK_TRIDENT_AGENT", "main")
    cmd = [
        openclaw,
        "agent",
        "--agent",
        agent_id,
        "--session-id",
        f"codex-stop-{session_id}-{turn_id}",
        "--message",
        prompt,
        "--thinking",
        "off",
        "--json",
        "--timeout",
        os.environ.get("CODEX_HOOK_TRIDENT_TIMEOUT", "600"),
    ]
    out = open(OUTGOING_LOG_PATH, "a")
    try:
        subprocess.Popen(
            cmd,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        out.close()
        return False, f"spawn failed: {exc}"
    out.close()
    return True, f"spawned background openclaw agent (output: {OUTGOING_LOG_PATH})"


def main() -> None:
    if os.environ.get("CODEX_HOOK_TRIDENT_NOTIFY", "1") == "0":
        log("disabled via env")
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception as exc:
        log(f"bad payload; fail-open: {exc}")
        return

    session_id = str(payload.get("session_id") or "unknown")
    turn_id = str(payload.get("turn_id") or "")
    verdict, preview = read_mimo_verdict(session_id, turn_id)
    prompt = build_prompt(payload, verdict, preview)
    sent, info = spawn_openclaw(payload, prompt)
    log(f"agent=codex session={short(session_id, 'unknown')} turn={short(turn_id, '')} mimo={verdict} sent={sent} info={info}")


if __name__ == "__main__":
    main()

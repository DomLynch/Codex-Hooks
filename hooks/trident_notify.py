#!/usr/bin/env python3
"""
Trident notify — Stop hook that pushes turn-end notifications to OpenClaw's Trident agent.

Replaces the 20-min polling cron with push-based notification. Codex/Claude finish a
turn → this hook waits briefly for MiMo's verdict → fires `openclaw agent --message`
to Trident with a compact summary. Trident decides what to do (queue work, ping user,
chain next task, just log).

Stdlib only. Always fail-open — never blocks Codex/Claude if OpenClaw is offline.

Stdin (Codex Stop hook payload):
  {"session_id": str, "transcript_path": str|null, "turn_id": str,
   "stop_hook_active": bool, "last_assistant_message": str|null, "cwd": str, ...}

Output: silent (exit 0). Logs to ~/.codex/logs/trident_notify.log.
"""
# ruff: noqa: E402
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- Tunables --------------------------------------------------------------
COMPONENT = "trident_notify"
AGENT_LABEL = os.environ.get("TRIDENT_AGENT_LABEL", "codex")  # "codex" or "claude"
MIMO_WAIT_TIMEOUT_S = 25     # how long to wait for MiMo verdict before sending anyway
OPENCLAW_TIMEOUT_S = 15      # how long openclaw agent invocation can take
SUMMARY_MAX_CHARS = 1200     # last_assistant_message preview length
ENABLE_ENV = "CODEX_HOOK_TRIDENT_NOTIFY"  # set to "0" to disable per-shell

# Path constants — Codex install. Claude install rewrites these to ~/.claude paths.
LOG_DIR = Path.home() / ".codex" / "logs"
MIMO_VERDICT_PATH = Path.home() / ".codex" / "state" / "mimo_last_verdict.json"
HOOK_ENV_PATH = Path.home() / ".codex" / "secrets" / "hook_env"

LOG_FILE = LOG_DIR / f"{COMPONENT}.log"


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def hook_env(name: str, default: str = "") -> str:
    """Mirror stop_orchestrator's env-or-file resolver. Env wins, then ~/.<install>/secrets/hook_env."""
    val = os.environ.get(name)
    if val is not None:
        return val
    try:
        for line in HOOK_ENV_PATH.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            if key.strip() == name:
                return raw.strip().strip("\"'")
    except Exception:
        pass
    return default


def wait_for_mimo_verdict(session_id: str, turn_id: str) -> dict:
    """Poll mimo_last_verdict.json until MiMo writes a matching turn_id, or timeout."""
    deadline = time.time() + MIMO_WAIT_TIMEOUT_S
    while time.time() < deadline:
        try:
            data = json.loads(MIMO_VERDICT_PATH.read_text())
            entry = data.get(session_id) or {}
            if entry and (not turn_id or entry.get("turn_id") == turn_id):
                return entry
        except Exception:
            pass
        time.sleep(1)
    return {}


def compose_message(payload: dict, mimo: dict) -> str:
    """Build the compact notification Trident receives."""
    last_msg = (payload.get("last_assistant_message") or "").strip()
    if len(last_msg) > SUMMARY_MAX_CHARS:
        last_msg = last_msg[: SUMMARY_MAX_CHARS // 2] + "\n...[truncated]...\n" + last_msg[-SUMMARY_MAX_CHARS // 2 :]

    verdict = mimo.get("verdict") or "UNKNOWN"
    preview = (mimo.get("preview") or "").strip()[:300]

    return (
        f"[{AGENT_LABEL.upper()} stop notification — Trident push hook]\n"
        f"agent: {AGENT_LABEL}\n"
        f"session_id: {payload.get('session_id', '?')}\n"
        f"turn_id: {payload.get('turn_id', '?')}\n"
        f"cwd: {payload.get('cwd', '?')}\n"
        f"mimo_verdict: {verdict}\n"
        f"mimo_preview: {preview}\n"
        f"\n--- last assistant message ---\n{last_msg}\n"
        f"--- end ---\n"
        f"\nDecide: queue follow-up work, ping the user, chain next task, or just log."
    )


def call_openclaw_agent(message: str) -> tuple[bool, str]:
    """Fire-and-forget invocation of `openclaw agent`. Spawns Trident in the background
    so we don't block the Stop hook on Trident's full turn (which can take >15s).
    Returns (ok, info)."""
    binary = shutil.which("openclaw")
    if not binary:
        return False, "openclaw binary not on PATH"

    # Append openclaw agent stdout/stderr to a sidecar log so we can debug delivery.
    out_log = LOG_DIR / "trident_notify_outgoing.log"
    try:
        out_log.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(out_log, "a")
        log_fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} fire ---\n")
        log_fh.flush()
        subprocess.Popen(
            [binary, "agent", "--agent", "main", "--message", message, "--json"],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach so child outlives this Stop hook
            close_fds=True,
        )
    except Exception as e:
        return False, f"spawn error: {e}"
    return True, f"spawned background openclaw agent (output: {out_log})"


def main() -> None:
    if hook_env(ENABLE_ENV, "1") == "0":
        log("disabled via env")
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"bad payload: {e}")
        sys.exit(0)

    session_id = payload.get("session_id") or "unknown"
    turn_id = payload.get("turn_id") or ""

    mimo = wait_for_mimo_verdict(session_id, turn_id)
    if not mimo:
        log(f"mimo verdict not available within {MIMO_WAIT_TIMEOUT_S}s — sending without")

    message = compose_message(payload, mimo)
    ok, info = call_openclaw_agent(message)
    log(
        f"agent={AGENT_LABEL} session={session_id[:12]} turn={turn_id[:12]} "
        f"mimo={mimo.get('verdict', 'NONE')} sent={ok} info={info}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

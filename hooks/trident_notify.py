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
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.transcript import latest_task_slice  # noqa: E402

# --- Tunables --------------------------------------------------------------
COMPONENT = "trident_notify"
AGENT_LABEL = os.environ.get("TRIDENT_AGENT_LABEL", "codex")  # "codex" or "claude"
MIMO_WAIT_TIMEOUT_S = 40     # under the 45s hook timeout in config.toml — leaves ~5s for compose+spawn. Doubles original 25s to catch most slow audits without exceeding the hook deadline.
OPENCLAW_TIMEOUT_S = int(os.environ.get("TRIDENT_OPENCLAW_TIMEOUT_S", "120"))
OPENCLAW_SESSION_ID = os.environ.get("TRIDENT_OPENCLAW_SESSION_ID", f"trident-notify-{AGENT_LABEL}")
SUMMARY_MAX_CHARS = 1200     # last_assistant_message preview length
TASK_SLICE_MAX_CHARS = 4000  # current task context sent to OpenClaw/Trident
ENABLE_ENV = "CODEX_HOOK_TRIDENT_NOTIFY"  # set to "0" to disable per-shell

# Coalesce noisy PASS/UNKNOWN notifications per session. ISSUES bypasses this:
# blocker signals must always reach Trident. 0 disables the debounce.
COOLDOWN_S = int(os.environ.get("TRIDENT_NOTIFY_COOLDOWN_S", "30"))

# Path constants — Codex install. Claude install rewrites these to ~/.claude paths.
LOG_DIR = Path.home() / ".codex" / "logs"
MIMO_VERDICT_PATH = Path.home() / ".codex" / "state" / "mimo_last_verdict.json"
HOOK_ENV_PATH = Path.home() / ".codex" / "secrets" / "hook_env"
COOLDOWN_PATH = Path.home() / ".codex" / "state" / "trident_notify_cooldown.json"

# OpenClaw leaves `<sid>.jsonl.lock` files if an agent process dies mid-write.
# Sweep only locks whose recorded pid is gone; live locks are preserved.
OPENCLAW_SESSIONS_DIR = Path.home() / ".openclaw" / "agents" / "main" / "sessions"

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


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def cleanup_stale_session_locks() -> int:
    if not OPENCLAW_SESSIONS_DIR.exists():
        return 0
    removed = 0
    for lock_path in OPENCLAW_SESSIONS_DIR.glob("*.jsonl.lock"):
        pid = 0
        try:
            data = json.loads(lock_path.read_text())
            pid = int(data.get("pid", 0) or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if pid > 0 and is_pid_alive(pid):
            continue
        try:
            lock_path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def should_send(session_id: str, force: bool) -> tuple[bool, str]:
    if force:
        return True, "force"
    if COOLDOWN_S <= 0:
        return True, "cooldown_disabled"
    now = time.time()
    state: dict = {}
    try:
        if COOLDOWN_PATH.exists():
            state = json.loads(COOLDOWN_PATH.read_text())
            if not isinstance(state, dict):
                state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    last = float(state.get(session_id, 0) or 0)
    if last and (now - last) < COOLDOWN_S:
        return False, f"cooldown {now - last:.1f}s of {COOLDOWN_S}s"
    state[session_id] = now
    cutoff = now - (COOLDOWN_S * 10)
    state = {k: v for k, v in state.items() if isinstance(v, (int, float)) and v >= cutoff}
    try:
        COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = COOLDOWN_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(COOLDOWN_PATH)
    except OSError:
        pass
    return True, "fired"


def wait_for_mimo_verdict(session_id: str, turn_id: str) -> dict:
    """Poll mimo_last_verdict.json until MiMo writes a matching turn_id, or timeout."""
    if not turn_id:
        return {}
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


def synthesize_turn_id(payload: dict) -> str:
    existing = str(payload.get("turn_id") or "").strip()
    if existing:
        return existing
    parts = [str(payload.get("session_id") or "unknown")]
    transcript_path = str(payload.get("transcript_path") or "")
    try:
        st = Path(transcript_path).stat() if transcript_path else None
        if st:
            parts.extend([str(int(st.st_size)), str(int(st.st_mtime))])
    except Exception:
        pass
    if len(parts) == 1:
        parts.extend([
            str(payload.get("cwd") or ""),
            str(payload.get("last_assistant_message") or "")[:300],
            str(int(time.time() / 60)),
        ])
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


def current_task_slice(payload: dict) -> str:
    return latest_task_slice(
        str(payload.get("transcript_path") or ""),
        fallback=str(payload.get("last_assistant_message") or ""),
        max_chars=TASK_SLICE_MAX_CHARS,
    ).strip()


def trident_instruction(verdict: str) -> str:
    normalized = (verdict or "").strip().upper()
    if normalized == "PASS":
        return (
            "MiMo PASS: the agent's turn was accepted. You may log it, queue a small "
            "reversible follow-up, or let the normal autonomy queue continue."
        )
    if normalized == "ISSUES":
        return (
            "MiMo ISSUES: the agent is being forced to fix this turn. Do NOT queue "
            "new work for this session yet; log the blocker and wait for the corrected PASS."
        )
    return (
        "MiMo verdict unavailable or unknown: log only unless there is already a "
        "clearly safe queued task."
    )


def compose_message(payload: dict, mimo: dict) -> str:
    """Build the compact notification Trident receives."""
    last_msg = (payload.get("last_assistant_message") or "").strip()
    if len(last_msg) > SUMMARY_MAX_CHARS:
        last_msg = last_msg[: SUMMARY_MAX_CHARS // 2] + "\n...[truncated]...\n" + last_msg[-SUMMARY_MAX_CHARS // 2 :]
    task_slice = (payload.get("_task_slice") or current_task_slice(payload) or "").strip()

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
        f"\n--- current task slice ---\n{task_slice or '(empty)'}\n"
        f"--- end current task slice ---\n"
        f"\n--- last assistant message ---\n{last_msg}\n"
        f"--- end ---\n"
        f"\nTrident instruction:\n{trident_instruction(str(verdict))}"
    )


def call_openclaw_agent(message: str) -> tuple[bool, str]:
    """Fire-and-forget invocation of `openclaw agent`. Spawns Trident in the background
    so we don't block the Stop hook on Trident's full turn (which can take >15s).
    Returns (ok, info)."""
    binary = shutil.which("openclaw")
    if not binary:
        return False, "openclaw binary not on PATH"

    # Append openclaw agent stdout/stderr to a sidecar log so we can debug delivery.
    # `with` ensures the parent's fd closes whether Popen succeeds or raises (no fd leak).
    # Popen dup'd the fd into the child's stdout, so closing in the parent is safe — the
    # background process keeps writing to its own descriptor.
    out_log = LOG_DIR / "trident_notify_outgoing.log"
    try:
        out_log.parent.mkdir(parents=True, exist_ok=True)
        with open(out_log, "a") as log_fh:
            log_fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} fire ---\n")
            log_fh.flush()
            subprocess.Popen(
                [
                    binary,
                    "agent",
                    "--agent",
                    "main",
                    "--session-id",
                    OPENCLAW_SESSION_ID,
                    "--thinking",
                    "minimal",
                    "--timeout",
                    str(OPENCLAW_TIMEOUT_S),
                    "--message",
                    message,
                    "--json",
                ],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach so child outlives this Stop hook
                close_fds=True,
            )
    except OSError as e:
        # Narrow to OSError — covers FileNotFoundError, PermissionError, the Popen failure
        # modes we actually expect. Lets unrelated bugs (TypeError, MemoryError) crash loudly.
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
    turn_id = synthesize_turn_id(payload)
    payload["turn_id"] = turn_id
    payload["_task_slice"] = current_task_slice(payload)

    mimo = wait_for_mimo_verdict(session_id, turn_id)
    if not mimo:
        log(f"mimo verdict not available within {MIMO_WAIT_TIMEOUT_S}s — sending without")

    verdict_str = str(mimo.get("verdict", "")).strip().upper()
    allow, reason = should_send(session_id, force=(verdict_str == "ISSUES"))
    if not allow:
        log(
            f"agent={AGENT_LABEL} session={session_id[:12]} turn={turn_id[:12]} "
            f"mimo={mimo.get('verdict', 'NONE')} debounced ({reason})"
        )
        sys.exit(0)

    try:
        cleaned = cleanup_stale_session_locks()
        if cleaned:
            log(f"swept {cleaned} stale openclaw session lock(s)")
    except Exception as e:
        log(f"lock cleanup failed (non-fatal): {e}")

    message = compose_message(payload, mimo)
    ok, info = call_openclaw_agent(message)
    log(
        f"agent={AGENT_LABEL} session={session_id[:12]} turn={turn_id[:12]} "
        f"mimo={mimo.get('verdict', 'NONE')} sent={ok} info={info} reason={reason}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

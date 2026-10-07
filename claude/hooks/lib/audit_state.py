"""Stop-audit state kept across Stops: per-turn iteration counts, the blocked turn id, consecutive-block and
reviewer-error counters, the last verdict per session, and escalation records."""
from __future__ import annotations

import time
from pathlib import Path

from .api import AUDITOR_NAME
from .json_state import load_json_locked, update_json_locked

STATE_FILE = Path.home() / ".claude" / "state" / "audit_iterations.json"
VERDICT_FILE = Path.home() / ".claude" / "state" / "mimo_last_verdict.json"
ESCALATION_DIR = Path.home() / ".claude" / "state" / "escalations"
CONSECUTIVE_BLOCK_PREFIX = "consecutive_blocks:"
AUDIT_ERROR_PREFIX = "audit_errors:"
PRESERVED_PREFIXES = (CONSECUTIVE_BLOCK_PREFIX, AUDIT_ERROR_PREFIX, "blocked_turn:")  # never evicted by save_iterations()


# Each hook run is a fresh process, so import time is when this Stop attempt started. save_verdict stamps it on the
# verdict so stop_orchestrator can tell this attempt's verdict from one an earlier attempt left (an hour-old PASS
# reached commit/push/PR, 2026-10-07 audit).
ATTEMPT_STARTED_AT = time.time()


def iteration_key(session_id: str, turn_id: str) -> str:
    return f"{session_id}:{turn_id or 'unknown-turn'}"


def load_iterations(session_id: str, turn_id: str) -> int:
    data = load_json_locked(STATE_FILE, {})
    key = iteration_key(session_id, turn_id)
    return _count(data.get(key))


def _count(value) -> int:
    return int(value.get("n", 0)) if isinstance(value, dict) else int(value or 0)


def _stamp(value) -> float:
    return float(value.get("t", 0)) if isinstance(value, dict) else 0.0   # legacy plain ints are the oldest


def save_iterations(session_id: str, turn_id: str, count: int) -> None:
    def updater(data: dict) -> dict:
        data[iteration_key(session_id, turn_id)] = {"n": count, "t": time.time()}
        # Remove legacy session-wide counters so old failures cannot poison new turns.
        data.pop(session_id, None)
        if len(data) > 50:
            # Truncate per-turn keys only. Damper counters are inserted early and would
            # otherwise be the first thing evicted, silently disabling the block-chain damper.
            dampers = {k: v for k, v in data.items() if k.startswith(PRESERVED_PREFIXES)}
            turns = {k: v for k, v in data.items() if not k.startswith(PRESERVED_PREFIXES)}
            # By recency: the file is saved with sort_keys, so dict order is key order, and a low-sorting turn key
            # was evicted the moment it was written (its count reset, so the iteration cap never counted up).
            data = {**dampers, **dict(sorted(turns.items(), key=lambda kv: _stamp(kv[1]))[-50:])}
        return data

    update_json_locked(STATE_FILE, updater)


def blocked_turn_key(session_id: str) -> str:
    return f"blocked_turn:{session_id}"


def remember_blocked_turn(session_id: str, turn_id: str) -> None:
    update_json_locked(STATE_FILE, lambda data: {**data, blocked_turn_key(session_id): turn_id})


def retry_turn_id(session_id: str, payload: dict, turn_id: str) -> str:
    """On a Stop retry (stop_hook_active), keep the turn id of the attempt we blocked.
    The synthesized id hashes the transcript size, and every 'Idle.' the lane appends after a block changes it,
    so iterations restarted at 0 on each retry and MAX_ITERATIONS never released the turn: Claude Code's own cap
    ended a Finishers turn after 9 blocks on 2026-09-22 12:5x. With a stable id the per-turn cap works again."""
    if not payload.get("stop_hook_active"):
        return turn_id
    remembered = load_json_locked(STATE_FILE, {}).get(blocked_turn_key(session_id))
    return str(remembered) if remembered else turn_id


def consecutive_block_key(session_id: str) -> str:
    return f"{CONSECUTIVE_BLOCK_PREFIX}{session_id or 'unknown'}"


def reset_consecutive_blocks(session_id: str) -> None:
    def updater(data: dict) -> dict:
        data.pop(consecutive_block_key(session_id), None)
        return data

    update_json_locked(STATE_FILE, updater)


def increment_consecutive_blocks(session_id: str) -> int:
    key = consecutive_block_key(session_id)

    def updater(data: dict) -> dict:
        data[key] = int(data.get(key, 0)) + 1
        return data

    return int(update_json_locked(STATE_FILE, updater).get(key, 0))


def audit_error_key(session_id: str) -> str:
    return f"{AUDIT_ERROR_PREFIX}{session_id or 'unknown'}"


def reset_audit_errors(session_id: str) -> None:
    def updater(data: dict) -> dict:
        data.pop(audit_error_key(session_id), None)
        return data

    update_json_locked(STATE_FILE, updater)


def increment_audit_errors(session_id: str) -> int:
    key = audit_error_key(session_id)

    def updater(data: dict) -> dict:
        data[key] = int(data.get(key, 0)) + 1
        return data

    return int(update_json_locked(STATE_FILE, updater).get(key, 0))


def write_escalation(session_id: str, cwd: str, message: str, attempts: int) -> Path:
    """Human-readable record of an unresolved release. Lives in state, path is logged."""
    ESCALATION_DIR.mkdir(parents=True, exist_ok=True)
    path = ESCALATION_DIR / f"{session_id or 'unknown'}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(
        f"# Audit escalation\n\n"
        f"- session: `{session_id}`\n- cwd: `{cwd}`\n- when: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"- reviewer: {AUDITOR_NAME}\n- consecutive failed audits: {attempts}\n\n"
        f"## Last error\n\n```\n{message[:2000]}\n```\n\n"
        f"The turn was released **unresolved** so the session is not bricked while the reviewer is down. "
        f"Nothing in this turn was reviewed. Re-run the audit or review the work manually.\n"
    )
    return path


def save_verdict(session_id: str, turn_id: str, verdict: str, preview: str = "") -> None:
    def updater(data: dict) -> dict:
        data[session_id] = {
            "turn_id": turn_id,
            "verdict": verdict,
            "preview": preview[:500],
            "updated_at": time.time(),
            "started_at": ATTEMPT_STARTED_AT,
        }
        if len(data) > 50:   # by recency: the file is saved with sort_keys, so dict order is session-id order, not age
            data = dict(sorted(data.items(), key=lambda kv: float(kv[1].get("updated_at") or 0) if isinstance(kv[1], dict) else 0.0)[-50:])
        return data

    update_json_locked(VERDICT_FILE, updater)

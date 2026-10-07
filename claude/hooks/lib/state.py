from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_DIR = Path.home() / ".claude" / "state"
HANDOFF_PATH = STATE_DIR / "handoff.json"
CHECKPOINT_PATH = STATE_DIR / "checkpoint_state.json"
SESSION_SPECS_DIR = STATE_DIR / "session_specs"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def load_handoff() -> dict:
    return load_json(HANDOFF_PATH, {})


def write_handoff(data: dict) -> None:
    data["last_updated_at"] = now_iso()
    write_json(HANDOFF_PATH, data)


def session_spec_path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id or "unknown")
    return SESSION_SPECS_DIR / f"{safe}.md"


def write_session_spec(session_id: str, prompt: str) -> None:
    path = session_spec_path(session_id)
    if path.exists() or not prompt.strip():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt.strip() + "\n")


def read_session_spec(session_id: str) -> str:
    path = session_spec_path(session_id)
    try:
        return path.read_text()
    except Exception:
        return ""


def checkpoint_state(session_id: str) -> dict:
    data = load_json(CHECKPOINT_PATH, {})
    return data.get(session_id, {"edits_since_last_commit": 0, "last_commit_at": ""})


def update_checkpoint_state(
    session_id: str,
    edits: int | None = None,
    committed: bool = False,
    branch_failed: bool = False,
    files: list | None = None,
) -> None:
    from .json_state import update_json_locked

    def apply(data: dict) -> dict:
        state = data.get(session_id)
        if not isinstance(state, dict):
            state = {"edits_since_last_commit": 0, "last_commit_at": ""}
        if edits is not None:
            state["edits_since_last_commit"] = max(0, int(state.get("edits_since_last_commit", 0)) + edits)
        if files:
            state["files"] = sorted({*state.get("files", []), *map(str, files)})
        if branch_failed:
            state["last_branch_failure_at"] = now_iso()
        if committed:
            state.pop("files", None)
            state["edits_since_last_commit"] = 0
            state["last_commit_at"] = now_iso()
            state.pop("last_branch_failure_at", None)
        data[session_id] = state
        return data

    update_json_locked(CHECKPOINT_PATH, apply)

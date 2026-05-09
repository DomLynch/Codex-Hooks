from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


def load_json_locked(path: Path, default: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        f.seek(0)
        raw = f.read()
        fcntl.flock(f, fcntl.LOCK_UN)
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def update_json_locked(path: Path, updater: Callable[[dict], dict]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load_json_locked(path, {})
        if not isinstance(data, dict):
            data = {}
        updated = updater(data)
        write_json_atomic(path, updated)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return updated

from __future__ import annotations

import time
from pathlib import Path


LOG_DIR = Path.home() / ".claude" / "logs"
# 2026-10-09: stop_orchestrator.log reached 24 MB and mimo_audit.log 12 MB, never rotated. Rotate at the cap, keep 3.
LOG_MAX_BYTES = 5_000_000
LOG_KEEP = 3


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < LOG_MAX_BYTES:
            return
        for i in range(LOG_KEEP, 1, -1):
            older = path.with_name(f"{path.name}.{i - 1}")
            if older.exists():
                older.replace(path.with_name(f"{path.name}.{i}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass   # another hook rotated first, or the file is gone; logging must never fail a hook


def log(component: str, msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{component}.log"
    _rotate(path)
    with open(path, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

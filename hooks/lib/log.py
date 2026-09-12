from __future__ import annotations

import time
from pathlib import Path
from lib.telemetry import report_failure


LOG_DIR = Path.home() / ".codex" / "logs"


def log(component: str, msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{component}.log"
    with open(path, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    if msg.startswith(("fail-open:", "gate error:", "quality error:",
                       "mimo checkpoint fail-open:", "claude audit fail-closed:")):
        report_failure(component, "hook_exception")

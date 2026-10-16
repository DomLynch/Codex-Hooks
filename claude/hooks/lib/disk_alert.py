"""One-line Mac free-space warning for the prompt hook (Strategy/Dom, 2026-10-16: the Mac hit 94% disk with 65 side
worktrees). Under 30 GB free, each lane hears it once an hour and is told the biggest directory it could move.
The directory size comes from a `du` that runs detached and is cached for an hour, so a prompt never waits for it."""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.json_state import update_json_locked  # noqa: E402

GB = 1024 ** 3
THRESHOLD_GB = 30
HOUR = 3600
REFRESH_GUARD_S = 600      # a refresh that started this recently is assumed still running
STATE = Path.home() / ".claude" / "state" / "disk_alert.json"
PARENTS = [Path.home() / "Developer", Path.home() / "Library" / "Application Support" / "Claude"]


def biggest_offender(parents: list[Path]) -> tuple[str, int] | None:
    """The largest immediate child directory of the given parents, as (path, bytes)."""
    best: tuple[str, int] | None = None
    for parent in parents:
        for child in sorted(parent.iterdir()) if parent.is_dir() else []:
            if not child.is_dir() or child.is_symlink():
                continue
            try:
                out = subprocess.run(["du", "-sk", "-x", str(child)], capture_output=True, text=True, timeout=180).stdout
                size = int(out.split()[0]) * 1024
            except (subprocess.SubprocessError, ValueError, IndexError, OSError):
                continue
            if best is None or size > best[1]:
                best = (str(child), size)
    return best


def save_offender(state: Path, path: str, gb: float, now: float | None = None) -> None:
    stamp = time.time() if now is None else now

    def updater(data: dict) -> dict:
        data["offender"] = {"path": path, "gb": gb, "t": stamp}
        data.pop("refreshing", None)
        return data

    update_json_locked(state, updater)


def _spawn_refresh(state: Path) -> None:
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--refresh", str(state)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def disk_line(session: str, state: Path = STATE, free_bytes: int | None = None, now: float | None = None,
              spawn: Callable[[Path], object] = _spawn_refresh) -> str:
    """The warning for this lane, or '' (enough space, already told this hour, or anything failed: never block a prompt)."""
    try:
        free = shutil.disk_usage(Path.home()).free if free_bytes is None else free_bytes
        if free >= THRESHOLD_GB * GB:
            return ""
        stamp = time.time() if now is None else now
        result: dict = {}

        def updater(data: dict) -> dict:
            seen = data.setdefault("seen", {})
            last = seen.get(session)
            if last is not None and stamp - last < HOUR:
                return data
            seen[session] = stamp
            if len(seen) > 60:
                data["seen"] = dict(sorted(seen.items(), key=lambda kv: kv[1])[-60:])
            offender = data.get("offender") or {}
            result["told"] = True
            result["offender"] = offender
            if (not offender or stamp - offender.get("t", 0) >= HOUR) and stamp - data.get("refreshing", -REFRESH_GUARD_S) >= REFRESH_GUARD_S:
                data["refreshing"] = stamp
                result["spawn"] = True
            return data

        update_json_locked(state, updater)
        if not result.get("told"):
            return ""
        if result.get("spawn"):
            spawn(state)
        text = f"Mac disk {free / GB:.0f} GB free: no large copies or builds here, move work to the VPS."
        offender = result.get("offender") or {}
        if offender.get("path"):
            text += f" Biggest: {offender['path']} ({offender.get('gb', 0):.0f} GB)."
        return text
    except Exception:
        return ""


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--refresh":
        found = biggest_offender(PARENTS)
        if found:
            save_offender(Path(sys.argv[2]), found[0], found[1] / GB)

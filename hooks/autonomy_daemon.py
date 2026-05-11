#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import (  # noqa: E402
    STATUS_PATH,
    autonomy_level,
    enabled,
    enqueue_tasks,
    queued_count,
    recent_repos,
    record_lesson,
)
from lib.json_state import update_json_locked  # noqa: E402
from lib.log import log  # noqa: E402


COMPONENT = "autonomy_daemon"
MIN_QUEUE = 3
MAX_NEW_TASKS = 5


def safe_run(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return 1, "", str(exc)


def mine_local_tasks(repo: Path, limit: int = 5) -> list[dict]:
    tasks: list[dict] = []
    if not (repo / ".git").exists():
        return tasks
    rg = shutil.which("rg")
    if rg:
        rc, out, _ = safe_run([rg, "-n", "--glob", "!node_modules", "--glob", "!.git", "TODO|FIXME|XXX"], repo)
        if rc in {0, 1}:
            for line in out.splitlines()[:limit]:
                path = line.split(":", 1)[0]
                tasks.append({
                    "title": f"Resolve backlog marker in {path}",
                    "body": f"Inspect `{path}`, resolve the TODO/FIXME if still valid, and run the relevant focused check.",
                    "cwd": str(repo),
                    "source_file": path,
                    "priority": 60,
                })
    if len(tasks) < limit and (repo / "tests").exists():
        rc, out, _ = safe_run(["git", "diff", "--name-only", "HEAD"], repo)
        if rc == 0:
            for path in [p for p in out.splitlines() if p.endswith((".py", ".ts", ".tsx", ".js"))][: limit - len(tasks)]:
                tasks.append({
                    "title": f"Add or tighten coverage for {path}",
                    "body": f"Review `{path}` for changed behavior, add the smallest missing test, and run that test.",
                    "cwd": str(repo),
                    "source_file": path,
                    "priority": 55,
                })
    if not tasks:
        if (repo / "package.json").exists():
            tasks.append({
                "title": "Run the project test script and fix the first concrete failure",
                "body": "Run the repo's existing test command, localize the first real failure, apply the smallest reversible fix, then rerun the focused check.",
                "cwd": str(repo),
                "source_file": "package.json",
                "priority": 45,
            })
        elif (repo / "tests").exists() or (repo / "pyproject.toml").exists():
            tasks.append({
                "title": "Run the Python test suite and fix the first concrete failure",
                "body": "Run the repo's existing Python tests, localize the first real failure, apply the smallest reversible fix, then rerun the focused check.",
                "cwd": str(repo),
                "source_file": "tests",
                "priority": 45,
            })
    return tasks[:limit]


def update_status(**fields: object) -> None:
    def updater(data: dict) -> dict:
        data.update(fields)
        data["updated_at"] = time.time()
        return data

    update_json_locked(STATUS_PATH, updater)


def watchdog() -> None:
    status = {}
    try:
        status = json.loads(STATUS_PATH.read_text())
    except Exception:
        pass
    last_success = float(status.get("last_enqueue_at") or 0)
    if last_success and time.time() - last_success > 6 * 60 * 60:
        record_lesson("watchdog", "Autonomy daemon has not enqueued work for 6h; inspect logs before increasing autonomy.")
    update_status(last_watchdog_at=time.time(), queued=queued_count(), level=autonomy_level())
    log(COMPONENT, f"watchdog queued={queued_count()} level={autonomy_level()}")


def run_once() -> int:
    if not enabled():
        log(COMPONENT, "disabled or AUTONOMY_STOP present")
        return 0
    current = queued_count()
    if current >= MIN_QUEUE:
        log(COMPONENT, f"queue healthy queued={current}")
        update_status(queued=current, level=autonomy_level())
        return 0
    added = 0
    for repo in recent_repos(limit=3):
        tasks = mine_local_tasks(repo, limit=MAX_NEW_TASKS)
        added += enqueue_tasks(tasks, source="local")
        if queued_count() >= MIN_QUEUE:
            break
    if added:
        update_status(last_enqueue_at=time.time(), queued=queued_count(), level=autonomy_level())
    else:
        update_status(last_empty_at=time.time(), queued=queued_count(), level=autonomy_level())
    log(COMPONENT, f"run queued_before={current} added={added} queued_after={queued_count()} level={autonomy_level()}")
    return added


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "watchdog":
        watchdog()
        return
    run_once()


if __name__ == "__main__":
    main()

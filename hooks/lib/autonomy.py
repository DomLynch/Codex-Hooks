from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from lib.json_state import load_json_locked, update_json_locked


STATE_DIR = Path.home() / ".codex" / "state"
SECRETS_ENV = Path.home() / ".codex" / "secrets" / "hook_env"
QUEUE_PATH = STATE_DIR / "autonomy_queue.json"
REPOS_PATH = STATE_DIR / "autonomy_repos.json"
LESSONS_JSONL = STATE_DIR / "lessons.jsonl"
LESSONS_ACTIVE = STATE_DIR / "lessons_active.md"
STATUS_PATH = STATE_DIR / "autonomy_status.json"
STOP_PATH = STATE_DIR / "AUTONOMY_STOP"
LEASE_TTL_S = 2 * 60 * 60
MAX_QUEUE = 80


def hook_env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    try:
        for line in SECRETS_ENV.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            if key.strip() == name:
                return raw.strip().strip("\"'")
    except Exception:
        pass
    return default


def enabled() -> bool:
    return hook_env("CODEX_HOOK_AUTONOMY", "1") != "0" and not STOP_PATH.exists()


def task_id(task: dict[str, Any]) -> str:
    base = "|".join(str(task.get(k, "")) for k in ("cwd", "title", "body", "source_file"))
    return hashlib.sha1(base.encode()).hexdigest()[:12]


def normalize_task(task: dict[str, Any], source: str) -> dict[str, Any]:
    title = str(task.get("title") or task.get("description") or "").strip()
    body = str(task.get("body") or task.get("acceptance") or "").strip()
    cwd = str(task.get("cwd") or "").strip()
    normalized = {
        "title": title[:160],
        "body": body[:1200],
        "cwd": cwd,
        "source_file": str(task.get("source_file") or ""),
        "priority": int(task.get("priority") or 50),
        "source": source,
        "status": "queued",
        "attempts": int(task.get("attempts") or 0),
        "created_at": time.time(),
    }
    normalized["id"] = str(task.get("id") or task_id(normalized))
    return normalized


def queue_state() -> dict[str, Any]:
    state = load_json_locked(QUEUE_PATH, {"version": 1, "tasks": []})
    if not isinstance(state, dict):
        return {"version": 1, "tasks": []}
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        state["tasks"] = []
    state.setdefault("version", 1)
    return state


def queued_count() -> int:
    return sum(1 for task in queue_state().get("tasks", []) if task.get("status") == "queued")


def enqueue_tasks(tasks: list[dict[str, Any]], source: str) -> int:
    if not tasks or not enabled():
        return 0
    added = 0

    def updater(state: dict) -> dict:
        nonlocal added
        state.setdefault("version", 1)
        current = state.setdefault("tasks", [])
        active_ids = {task.get("id") for task in current if task.get("status") in {"queued", "leased"}}
        for raw in tasks:
            task = normalize_task(raw, source)
            if not task["title"] or task["id"] in active_ids:
                continue
            current.append(task)
            active_ids.add(task["id"])
            added += 1
        state["tasks"] = sorted(current, key=lambda t: int(t.get("priority", 0)), reverse=True)[:MAX_QUEUE]
        state["updated_at"] = time.time()
        return state

    update_json_locked(QUEUE_PATH, updater)
    return added


def pop_next_task(worker: str, cwd: Path | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = time.time()
    wanted = str(cwd.resolve()) if cwd else ""
    picked: dict[str, Any] | None = None

    def updater(state: dict) -> dict:
        nonlocal picked
        tasks = state.setdefault("tasks", [])
        for task in tasks:
            if task.get("status") == "leased" and float(task.get("lease_until") or 0) < now:
                task["status"] = "queued"
                task.pop("worker", None)
                task.pop("lease_until", None)
        for task in tasks:
            if task.get("status") != "queued":
                continue
            task_cwd = str(task.get("cwd") or "")
            if wanted and task_cwd and Path(task_cwd).resolve() != Path(wanted):
                continue
            task["status"] = "leased"
            task["worker"] = worker
            task["leased_at"] = now
            task["lease_until"] = now + LEASE_TTL_S
            task["attempts"] = int(task.get("attempts") or 0) + 1
            picked = dict(task)
            break
        state["updated_at"] = now
        return state

    update_json_locked(QUEUE_PATH, updater)
    return picked


def remember_repo(cwd: Path) -> None:
    try:
        resolved = str(cwd.resolve())
    except Exception:
        return

    def updater(state: dict) -> dict:
        repos = state.setdefault("repos", {})
        repos[resolved] = {"path": resolved, "last_seen": time.time()}
        if len(repos) > 20:
            kept = sorted(repos.values(), key=lambda item: item.get("last_seen", 0))[-20:]
            state["repos"] = {item["path"]: item for item in kept}
        return state

    update_json_locked(REPOS_PATH, updater)


def recent_repos(limit: int = 5) -> list[Path]:
    state = load_json_locked(REPOS_PATH, {})
    repos = list((state.get("repos") or {}).values()) if isinstance(state, dict) else []
    paths = [Path(item["path"]) for item in sorted(repos, key=lambda x: x.get("last_seen", 0), reverse=True)]
    return [path for path in paths if path.exists()][:limit]


def record_lesson(source: str, text: str) -> None:
    clean = " ".join((text or "").split())[:240]
    if not clean:
        return
    LESSONS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(LESSONS_JSONL, "a") as f:
        f.write(json.dumps({"ts": time.time(), "source": source, "lesson": clean}) + "\n")
    active = active_lessons(10)
    if clean not in active:
        active.insert(0, clean)
    LESSONS_ACTIVE.write_text("\n".join(f"- {item}" for item in active[:5]) + "\n")


def active_lessons(limit: int = 5) -> list[str]:
    try:
        return [
            line[2:].strip()
            for line in LESSONS_ACTIVE.read_text().splitlines()
            if line.startswith("- ") and line[2:].strip()
        ][:limit]
    except Exception:
        return []


def autonomy_level() -> str:
    level = hook_env("CODEX_HOOK_AUTONOMY_LEVEL", "standard").lower()
    return level if level in {"conservative", "standard", "aggressive", "wild"} else "standard"

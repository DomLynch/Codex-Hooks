from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from lib.json_state import load_json_locked, update_json_locked


STATE_DIR = Path.home() / ".codex" / "state"
SECRETS_ENV = Path.home() / ".codex" / "secrets" / "hook_env"
QUEUE_PATH = STATE_DIR / "autonomy_queue.json"
REPOS_PATH = STATE_DIR / "autonomy_repos.json"
LESSONS_JSONL = STATE_DIR / "lessons.jsonl"
LESSONS_STATE = STATE_DIR / "lessons_active.json"
LESSONS_ACTIVE = STATE_DIR / "lessons_active.md"
STATUS_PATH = STATE_DIR / "autonomy_status.json"
STOP_PATH = STATE_DIR / "AUTONOMY_STOP"
LEASE_TTL_S = 2 * 60 * 60
MAX_QUEUE = 80
MAX_ACTIVE_LESSONS = 8
DEFAULT_LESSON_TTL_DAYS = 14
SKIP_REPO_PARTS = {".claude", ".codex", ".git", "node_modules"}
RISKY_TASK_RE = re.compile(
    r"\b("
    r"rm\s+-rf|delete|drop\s+(database|table|schema)|truncate|migrat(e|ion)|"
    r"prod(uction)?|deploy|vps|ssh|scp|rsync|terraform|dns|certificate|"
    r"secret|credential|token|api\s*key|payment|purchase|billing|sudo|chmod|chown|"
    r"curl\b.*\|\s*(sh|bash)|auto-?merge|merge\s+to\s+main"
    r")\b",
    re.IGNORECASE,
)


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


def _truthy(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "y", "approved"}


def is_safe_task(task: dict[str, Any]) -> tuple[bool, str]:
    if not str(task.get("title") or "").strip():
        return False, "missing task title"
    if _truthy(task.get("approved")):
        return True, "approved"
    if task.get("status") == "held" or _truthy(task.get("requires_approval")):
        return False, "task requires manual approval"
    if not str(task.get("cwd") or "").strip():
        return False, "missing repo cwd"
    haystack = "\n".join(str(task.get(key) or "") for key in ("title", "body", "source_file", "cwd"))
    if RISKY_TASK_RE.search(haystack):
        return False, "risky autonomous task; set approved=true after human review"
    return True, "safe"


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
        "approved": _truthy(task.get("approved")),
        "requires_approval": _truthy(task.get("requires_approval")),
        "created_at": time.time(),
    }
    normalized["id"] = str(task.get("id") or task_id(normalized))
    safe, reason = is_safe_task(normalized)
    normalized["safety"] = "approved" if normalized["approved"] else ("safe" if safe else "held")
    if not safe:
        normalized["status"] = "held"
        normalized["hold_reason"] = reason
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
        active_ids = {task.get("id") for task in current if task.get("status") in {"queued", "leased", "held"}}
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
            safe, reason = is_safe_task(task)
            if not safe:
                task["status"] = "held"
                task["safety"] = "held"
                task["hold_reason"] = reason
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
    if not (Path(resolved) / ".git").exists():
        return
    if set(Path(resolved).parts) & SKIP_REPO_PARTS:
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
    return [
        path for path in paths
        if (path / ".git").exists() and not (set(path.parts) & SKIP_REPO_PARTS)
    ][:limit]


def record_lesson(source: str, text: str) -> None:
    clean = " ".join((text or "").split())[:240]
    if not clean:
        return
    now = time.time()
    LESSONS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(LESSONS_JSONL, "a") as f:
        f.write(json.dumps({"ts": now, "source": source, "lesson": clean}) + "\n")

    def updater(state: dict) -> dict:
        entries = _fresh_lesson_entries(_lesson_entries(state), now)
        entries = [item for item in entries if item["lesson"] != clean]
        entries.insert(0, {"lesson": clean, "source": source, "ts": now})
        state["lessons"] = entries[:MAX_ACTIVE_LESSONS]
        state["updated_at"] = now
        LESSONS_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        LESSONS_ACTIVE.write_text("\n".join(f"- {item['lesson']}" for item in state["lessons"]) + "\n")
        return state

    update_json_locked(LESSONS_STATE, updater)


def lesson_ttl_s() -> int:
    try:
        days = int(hook_env("CODEX_HOOK_LESSON_TTL_DAYS", str(DEFAULT_LESSON_TTL_DAYS)))
    except ValueError:
        days = DEFAULT_LESSON_TTL_DAYS
    return max(1, days) * 24 * 60 * 60


def _lesson_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    updated_at = float(state.get("updated_at") or 0)
    entries: list[dict[str, Any]] = []
    for item in state.get("lessons", []) if isinstance(state.get("lessons"), list) else []:
        if isinstance(item, dict):
            lesson = str(item.get("lesson") or "").strip()
            if lesson:
                entries.append({
                    "lesson": lesson,
                    "source": str(item.get("source") or "unknown"),
                    "ts": float(item.get("ts") or updated_at or 0),
                })
        else:
            lesson = str(item).strip()
            if lesson:
                entries.append({"lesson": lesson, "source": "legacy", "ts": updated_at})
    return entries


def _fresh_lesson_entries(entries: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    current = time.time() if now is None else now
    cutoff = current - lesson_ttl_s()
    fresh = [item for item in entries if float(item.get("ts") or 0) >= cutoff]
    return fresh[:MAX_ACTIVE_LESSONS]


def active_lessons(limit: int = MAX_ACTIVE_LESSONS) -> list[str]:
    state = load_json_locked(LESSONS_STATE, {})
    if isinstance(state, dict) and isinstance(state.get("lessons"), list):
        return [item["lesson"] for item in _fresh_lesson_entries(_lesson_entries(state))][:limit]
    try:
        if time.time() - LESSONS_ACTIVE.stat().st_mtime > lesson_ttl_s():
            return []
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

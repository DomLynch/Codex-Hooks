"""OpenClaw session-lock hygiene helpers.

Background: OpenClaw's gateway writes per-session JSONL files protected by a
sibling `<session>.jsonl.lock`. When an agent process dies mid-write (crash,
SIGKILL, daemon restart leaving an orphan child), the lock file persists and
blocks the next writer for the full lock timeout (60s).

This module provides fail-open helpers for hooks to clean up after such
crashes. The default surface is **lock cleanup only**. Process reaping is
opt-in (env-gated) because OpenClaw legitimately runs *detached* agent
processes (e.g. fire-and-forget `openclaw agent --message ...` calls from
trident_notify) whose parent is *not* the gateway — so a parent-pid heuristic
alone cannot distinguish those from real orphans without the risk of killing
in-flight legitimate work.

Safety properties:
  - stdlib only
  - never raises into the caller (`safe_*` wrappers swallow OSError + parse errors)
  - idempotent on a clean directory (no-op cost beyond a stat per lock file)
  - cross-user safe: PermissionError on `os.kill(pid, 0)` is treated as "alive"
  - mtime-guarded: malformed/unreadable locks <5s old are preserved (could be
    mid-write, file system races)
  - reaper disabled by default; requires both `enable_reaper=True` AND
    `OPENCLAW_REAP_ORPHANS=1` env var (defense in depth)

Used by:
  - hooks/session_start.py  (one-shot cleanup at Claude Code session boot)
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterable

DEFAULT_AGENTS_ROOT = Path.home() / ".openclaw" / "agents"

MALFORMED_LOCK_MIN_AGE_SECONDS = 5
"""Lock files we can't parse are only treated as stale once they're at least
this old. Protects against unlinking a lock that's currently mid-write."""

REAP_MIN_AGE_SECONDS = 60
"""Process must be older than this to be considered for reaping. Long enough
that a normal openclaw-agent CLI call (which can legitimately take ~30s) has
either completed or is clearly hung."""

REAP_ENV_FLAG = "OPENCLAW_REAP_ORPHANS"
"""Env var that must be set to '1' to enable process reaping. Defense in depth
on top of `enable_reaper=True` — protects against accidentally enabling it via
default code path changes."""


# ---------- liveness ---------------------------------------------------------

def is_pid_alive(pid: int) -> bool:
    """Send signal 0 to test if a PID is currently a live process.

    Returns False for pid <= 0, dead PIDs (ProcessLookupError), and other
    OSErrors. Returns True for living PIDs we can signal AND for living PIDs
    owned by another user (PermissionError) — we deliberately treat
    not-our-process as alive so we never unlink someone else's lock.
    """
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


# ---------- lock cleanup -----------------------------------------------------

def _iter_session_dirs(agents_root: Path) -> Iterable[Path]:
    """Yield each `<agents_root>/<agent>/sessions/` dir that exists."""
    if not agents_root.exists():
        return
    for agent_dir in agents_root.iterdir():
        if not agent_dir.is_dir():
            continue
        sessions = agent_dir / "sessions"
        if sessions.is_dir():
            yield sessions


def _lock_age_seconds(lock_path: Path) -> float:
    """Lock file mtime → age in seconds. Returns -1 on stat failure."""
    try:
        return time.time() - lock_path.stat().st_mtime
    except OSError:
        return -1.0


def cleanup_stale_session_locks(
    agents_root: Path = DEFAULT_AGENTS_ROOT,
    *,
    malformed_min_age: float = MALFORMED_LOCK_MIN_AGE_SECONDS,
    log_fn=None,
) -> int:
    """Sweep `<agents_root>/*/sessions/*.jsonl.lock` and remove any whose
    recorded pid is no longer alive. Returns the number removed.

    Decision matrix per lock file:
      - parseable JSON, pid > 0, pid alive  → KEEP
      - parseable JSON, pid > 0, pid dead   → REMOVE
      - parseable JSON, pid 0 or negative   → REMOVE (corrupt)
      - unparseable / unreadable, age >= malformed_min_age → REMOVE
      - unparseable / unreadable, age <  malformed_min_age → KEEP (race window)

    Always idempotent: a clean tree is a no-op. Cross-user safe via
    `is_pid_alive`'s PermissionError handling.
    """
    removed = 0
    for sessions_dir in _iter_session_dirs(agents_root):
        for lock_path in sessions_dir.glob("*.jsonl.lock"):
            pid: int | None = None  # None = unparseable
            try:
                data = json.loads(lock_path.read_text())
                pid = int(data.get("pid", 0) or 0)
            except (OSError, ValueError, json.JSONDecodeError):
                pass

            if pid is None:
                # Unparseable: only remove if old enough that this can't be a
                # mid-write race. Fresh malformed locks are preserved.
                age = _lock_age_seconds(lock_path)
                if age < 0 or age < malformed_min_age:
                    continue
            elif pid > 0 and is_pid_alive(pid):
                continue

            try:
                lock_path.unlink()
                removed += 1
                if log_fn:
                    try:
                        log_fn(
                            f"removed stale lock {lock_path.name} "
                            f"(pid={pid if pid is not None else 'malformed'})"
                        )
                    except Exception:
                        pass
            except OSError:
                # Race with the legit owner re-acquiring, or perms — skip silently.
                pass
    return removed


# ---------- gateway pid discovery -------------------------------------------

def get_gateway_pids() -> list[int]:
    """Return PIDs of currently running OpenClaw gateway processes via
    `launchctl list`. Empty list on any failure."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        if "ai.openclaw.gateway" not in line:
            continue
        head, *_ = line.split()
        if head and head != "-":
            try:
                pids.append(int(head))
            except ValueError:
                pass
    return pids


# ---------- process reaping (opt-in only) -----------------------------------

def _proc_age_seconds(pid: int) -> int:
    """Return process elapsed-time in seconds, or 0 on parse failure / dead pid.
    Uses macOS/BSD-compatible `ps -o etime=`."""
    try:
        out = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 0
    if not out:
        return 0
    days = 0
    if "-" in out:
        d, out = out.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            pass
    parts = out.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 3:
        h, m, s = parts
    else:
        return 0
    return days * 86400 + h * 3600 + m * 60 + s


def reap_orphan_agents(
    *,
    gateway_pids: Iterable[int] | None = None,
    min_age_seconds: int = REAP_MIN_AGE_SECONDS,
    log_fn=None,
) -> int:
    """Kill `openclaw-agent` processes whose parent is not a live gateway AND
    that have been alive longer than `min_age_seconds`.

    KNOWN LIMITATION: cannot distinguish between (a) genuinely orphaned agent
    runs from a crashed gateway and (b) legitimate detached agent runs spawned
    by `trident_notify` (which are NOT gateway children — they're `start_new_session=True`).
    The age gate (default 60s) makes false-positives less likely but does not
    prevent them. **Caller should only invoke this when it is acceptable to
    occasionally interrupt a long-running detached agent run.**

    For that reason, the orchestrator `session_boot_cleanup()` does not call
    this function unless explicitly opted in via both `enable_reaper=True` AND
    `OPENCLAW_REAP_ORPHANS=1` env var.
    """
    gateway_set = set(gateway_pids or [])
    reaped = 0
    try:
        result = subprocess.run(
            ["pgrep", "-lf", "openclaw-agent|openclaw agent --"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 0
    if result.returncode not in (0, 1):
        return 0
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, *_ = line.split(maxsplit=1)
        try:
            pid = int(head)
        except ValueError:
            continue
        if pid in gateway_set or pid <= 0 or pid == os.getpid():
            continue
        age = _proc_age_seconds(pid)
        if age < min_age_seconds:
            continue
        try:
            ps = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            )
            ppid = int((ps.stdout or "0").strip() or 0)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
            ppid = 0
        if ppid in gateway_set and is_pid_alive(ppid):
            continue
        try:
            os.kill(pid, 9)
            reaped += 1
            if log_fn:
                try:
                    log_fn(f"reaped orphan agent pid={pid} ppid={ppid} age={age}s")
                except Exception:
                    pass
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if reaped > 0:
        time.sleep(0.2)
    return reaped


# ---------- orchestrator ----------------------------------------------------

def session_boot_cleanup(
    *,
    enable_reaper: bool = False,
    log_fn=None,
) -> dict:
    """SessionStart-hook entrypoint. Default is **lock cleanup only**.

    Reaping orphan processes is opt-in via `enable_reaper=True` AND env var
    `OPENCLAW_REAP_ORPHANS=1` (both required) because the heuristic can kill
    legitimate detached agent runs.

    Never raises — wraps the underlying calls so cleanup failures cannot block
    SessionStart.
    """
    summary = {"reaped_agents": 0, "removed_locks": 0}
    env_opt_in = os.environ.get(REAP_ENV_FLAG, "") == "1"
    try:
        if enable_reaper and env_opt_in:
            summary["reaped_agents"] = reap_orphan_agents(
                gateway_pids=get_gateway_pids(), log_fn=log_fn,
            )
        summary["removed_locks"] = cleanup_stale_session_locks(log_fn=log_fn)
    except Exception as exc:
        if log_fn:
            try:
                log_fn(f"session_boot_cleanup error (non-fatal): {exc}")
            except Exception:
                pass
    return summary

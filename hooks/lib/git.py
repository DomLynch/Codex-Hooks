from __future__ import annotations

import subprocess
import os
import re
import signal
from pathlib import Path


KILL_DRAIN_S = 5  # how long to wait for the pipes after killing the group; bounded so this can never wedge


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the child AND everything it spawned. Falls back to the child alone if the group is already gone."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def safe_run(
    cmd: list[str],
    cwd: str | Path | None = None,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    # Not subprocess.run(timeout=...): on POSIX that kills only the DIRECT child and then calls process.wait(),
    # so everything the child spawned is orphaned and keeps running. The Stop path runs `npm run test:changed`
    # (npm -> node), `pytest` (-> xdist workers) and `go test ./...` (-> test binaries), so a timed-out gate
    # leaves a live test suite behind on a Mac a dozen lanes share — the contention the deploy guard exists to
    # prevent (2026-09-21: two 100 s checks took 15 min each under load 30+). start_new_session puts the child
    # in its own process group so the timeout can kill the whole tree.
    # NOT a fix for the two 1,437.9s Stop runs on 2026-09-22: the theory that this call blocks draining the
    # pipes is false on POSIX and test_safe_run_process_group.py pins that. Those outliers are still unexplained.
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        return 1, "", str(exc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            out, err = proc.communicate(timeout=KILL_DRAIN_S)
        except Exception:
            out, err = "", ""
        return 1, out or "", f"timed out after {timeout}s; process group killed"
    except Exception as exc:
        _kill_process_group(proc)
        return 1, "", str(exc)


def is_git_repo(cwd: str | Path) -> bool:
    rc, out, _ = safe_run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return rc == 0 and out.strip() == "true"


def git_root(cwd: str | Path) -> Path | None:
    rc, out, _ = safe_run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if rc != 0 or not out.strip():
        return None
    return Path(out.strip()).resolve()


def _workdirs_from_text(text: str) -> list[Path]:
    paths: list[Path] = []
    for match in re.finditer(r'"workdir"\s*:\s*"([^"]+)"', text or ""):
        raw = match.group(1).replace("\\/", "/")
        paths.append(Path(raw).expanduser())
    for match in re.finditer(r"'workdir'\s*:\s*'([^']+)'", text or ""):
        paths.append(Path(match.group(1)).expanduser())
    return paths


def _absolute_paths_from_text(text: str) -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    # Keep this intentionally macOS/local-path focused. Remote VPS paths are
    # audit-only unless the hook process can actually access that filesystem.
    patterns = (
        r'["\'](/Users/[^"\']+)["\']',
        r"/Users/[^\"'`\n]+?\.(?:py|js|jsx|ts|tsx|go|rs|md|json|toml|yaml|yml|txt|sh|css|html)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text or ""):
            raw = match.group(1) if match.groups() else match.group(0)
            path = Path(raw.rstrip(".,;:)")).expanduser()
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def resolve_git_cwd(
    cwd: str | Path,
    files: list[Path] | tuple[Path, ...] | None = None,
    transcript: str = "",
) -> Path:
    """Return the actual repo root for hooks launched from a workspace folder.

    Codex Desktop often starts a thread from a parent workspace such as
    ~/Desktop/Business while tool calls edit a nested project repo. Side-effect
    hooks must operate on the nested repo, not the non-git workspace root.
    """
    base = Path(cwd).resolve()
    root = git_root(base)
    if root:
        return root

    candidates: list[Path] = []
    for file in files or []:
        probe = file.parent if file.suffix else file
        root = git_root(probe)
        if root and root not in candidates:
            candidates.append(root)
    for workdir in _workdirs_from_text(transcript):
        root = git_root(workdir)
        if root and root not in candidates:
            candidates.append(root)
    for path in _absolute_paths_from_text(transcript):
        probe = path if path.is_dir() else path.parent
        root = git_root(probe)
        if root and root not in candidates:
            candidates.append(root)

    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        for candidate in candidates:
            if str(candidate) in transcript or candidate.name in transcript:
                return candidate
    return base


def changed_files(cwd: str | Path) -> list[Path]:
    rc, out, _ = safe_run(["git", "diff", "--name-only"], cwd=cwd)
    if rc != 0:
        return []
    root = git_root(cwd) or Path(cwd)
    return [root / line.strip() for line in out.splitlines() if line.strip()]

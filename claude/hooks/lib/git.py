from __future__ import annotations

import subprocess
import os
import re
import signal
import time
from pathlib import Path


def short_sid(session_id: str) -> str:
    return (session_id or "unknown").replace("/", "-")[:8]


def current_session_branch(session_id: str) -> str:
    return f"claude/{short_sid(session_id)}/main"


KILL_DRAIN_S = 1  # how long to wait for the pipes after killing the group; bounded so this can never wedge


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the child AND everything it spawned. The child is its own group leader (start_new_session), so the group
    id is its pid even when the child already exited and only a grandchild is left."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _term_to_exit(signum, frame):
    raise SystemExit(128 + signum)


def safe_run(
    cmd: list[str],
    cwd: str | Path | None = None,
    timeout: int = 30,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    raise_on_timeout: bool = False,
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
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        return 1, "", str(exc)
    # A SIGTERM to the hook must unwind (and so kill the child group) instead of orphaning the child.
    try:
        old_term = signal.signal(signal.SIGTERM, _term_to_exit)
    except ValueError:   # not the main thread
        old_term = None
    deadline, pending = time.monotonic() + timeout, input_text
    try:
        while True:
            remaining = deadline - time.monotonic()
            try:
                out, err = proc.communicate(pending, timeout=max(0.0, min(0.2, remaining)))
                break
            except subprocess.TimeoutExpired:
                pending = None   # the input is already queued; a retry must not pass it again
                if proc.poll() is not None:   # the child is gone but a descendant still holds the pipes
                    out, err = _drain(proc)
                    break
                if remaining <= 0:
                    _kill_process_group(proc)
                    out, err = _drain(proc)
                    if raise_on_timeout:
                        raise TimeoutError(f"timed out after {timeout}s") from None
                    return 1, out, f"timed out after {timeout}s; process group killed"
        _kill_process_group(proc)   # leftovers of the child's own group do not outlive it
        return proc.returncode, out, err
    except TimeoutError:
        raise
    except Exception as exc:
        _kill_process_group(proc)
        return 1, "", str(exc)
    except BaseException:
        _kill_process_group(proc)
        raise
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)


def _drain(proc: subprocess.Popen) -> tuple[str, str]:
    _kill_process_group(proc)
    try:
        out, err = proc.communicate(timeout=KILL_DRAIN_S)
    except Exception:
        return "", ""
    return out or "", err or ""


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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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

    candidates: list[Path] = []
    for file in files or []:
        probe = file.parent if file.suffix else file
        file_root = git_root(probe)
        if file_root and file_root not in candidates:
            candidates.append(file_root)
    if root:
        # A session launched from one repo (the desktop app's project folder)
        # can spend the whole turn editing a worktree of another. The edited
        # files decide, but only when they all sit in one other repo and none
        # of them is in the launch repo: side effects, diffs and the quality
        # gate must run where the change actually is.
        if candidates == [root] or not candidates:
            return root
        if len(candidates) == 1 and not any(_is_relative_to(f.resolve(), root) for f in files or []):
            return candidates[0]
        return root
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


def current_branch(cwd: str | Path) -> str:
    rc, out, _ = safe_run(["git", "branch", "--show-current"], cwd=cwd)
    return out.strip() if rc == 0 else ""


def trunk_branch(cwd: str | Path) -> str:
    """origin/HEAD without the remote name: the branch PRs merge into. "" when unset or not a repo."""
    rc, out, _ = safe_run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd=cwd)
    return out.strip().removeprefix("origin/") if rc == 0 else ""


def ensure_branch(branch: str, cwd: str | Path) -> bool:
    if not is_git_repo(cwd):
        return False
    if current_branch(cwd) == branch:
        return True
    rc, target, _ = safe_run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=cwd)
    if rc == 0:
        # Automatic checkpoints must never replace the checkout with another revision.
        head_rc, head, _ = safe_run(["git", "rev-parse", "--verify", "HEAD"], cwd=cwd)
        if head_rc != 0 or head.strip() != target.strip():
            return False
        rc, _, _ = safe_run(["git", "checkout", branch], cwd=cwd)
        return rc == 0
    rc, _, _ = safe_run(["git", "checkout", "-b", branch], cwd=cwd)
    return rc == 0


def has_staged_changes(cwd: str | Path, paths: list[str]) -> bool:
    rc, _, _ = safe_run(["git", "diff", "--cached", "--quiet", "--", *paths], cwd=cwd)
    return rc == 1


def commit(message: str, cwd: str | Path, files: list) -> tuple[bool, str]:
    """Commit only `files` (the ones this session edited), never the rest of a shared checkout."""
    root = git_root(cwd)
    if not root:
        return False, ""
    paths = []
    for file in files:
        try:
            paths.append(str(Path(file).resolve().relative_to(root.resolve())))
        except ValueError:
            continue
    if not paths:
        return False, ""
    rc, _, _ = safe_run(["git", "add", "-A", "--", *paths], cwd=root)
    if rc != 0 or not has_staged_changes(root, paths):
        return False, ""
    rc, out, err = safe_run(["git", "commit", "-m", message, "--", *paths], cwd=root, timeout=60)
    if rc != 0:
        return False, err or out
    rc, sha, _ = safe_run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
    return True, sha.strip() if rc == 0 else ""


def push(branch: str, cwd: str | Path) -> bool:
    rc, _, _ = safe_run(["git", "push", "-u", "origin", branch], cwd=cwd, timeout=120)
    return rc == 0


def changed_files(cwd: str | Path) -> list[Path]:
    rc, out, _ = safe_run(["git", "diff", "--name-only"], cwd=cwd)
    if rc != 0:
        return []
    root = git_root(cwd) or Path(cwd)
    return [root / line.strip() for line in out.splitlines() if line.strip()]


def github_env() -> dict[str, str] | None:
    """Look up GitHub token from ~/.claude first, fall back to ~/.codex (shared)."""
    for base in (Path.home() / ".claude" / "secrets", Path.home() / ".codex" / "secrets"):
        token_path = base / "github_token"
        if token_path.exists():
            try:
                token = token_path.read_text().strip()
                if token:
                    return {**os.environ, "GH_TOKEN": token}
            except Exception:
                continue
    return None

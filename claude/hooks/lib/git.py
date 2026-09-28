from __future__ import annotations

import subprocess
import os
import re
import signal
from pathlib import Path


def short_sid(session_id: str) -> str:
    return (session_id or "unknown").replace("/", "-")[:8]


def current_session_branch(session_id: str) -> str:
    return f"claude/{short_sid(session_id)}/main"


def task_branch(session_id: str, task_n: int) -> str:
    return f"claude/{short_sid(session_id)}/task-{task_n}"


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


def has_worktree_changes(cwd: str | Path) -> bool:
    rc, out, _ = safe_run(["git", "status", "--porcelain"], cwd=cwd)
    return rc == 0 and bool(out.strip())


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


def files_changed_summary(cwd: str | Path) -> str:
    rc, out, _ = safe_run(["git", "diff", "--stat"], cwd=cwd)
    return out.strip() if rc == 0 else ""


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


def github_pr_preflight(cwd: str | Path) -> tuple[bool, str]:
    """Check PR creation prerequisites before local commit/push side effects."""
    rc, out, err = safe_run(["git", "remote", "get-url", "origin"], cwd=cwd, timeout=10)
    if rc != 0 or not out.strip():
        return False, "missing git origin remote"
    env = github_env()
    for cmd, label in (
        (["gh", "--version"], "gh CLI unavailable"),
        (["gh", "auth", "status"], "gh auth unavailable"),
        (["gh", "repo", "view", "--json", "nameWithOwner"], "gh repo unavailable"),
    ):
        rc, out, err = safe_run(cmd, cwd=cwd, timeout=20, env=env)
        if rc != 0:
            detail = (err or out).strip().splitlines()
            return False, label + (f": {detail[-1]}" if detail else "")
    return True, "ok"


def open_pr(title: str, body: str, base: str = "main", cwd: str | Path | None = None) -> str:
    env = github_env()
    rc, out, err = safe_run(
        ["gh", "pr", "create", "--base", base, "--title", title, "--body", body],
        cwd=cwd,
        timeout=120,
        env=env,
    )
    if rc != 0:
        return ""
    return out.strip()

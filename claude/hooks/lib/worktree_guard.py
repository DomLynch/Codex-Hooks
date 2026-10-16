"""Two side copies per lane, no more (Lead + Dom, 2026-10-16: "a universal fix so this wastage doesn't happen again").

The Mac reached 94% disk with 65 finished side copies (~70 GB) of ~/Developer/frankendom-main. `worktree_add_reason` refuses
`git worktree add <path>` when <path> is ~/Developer/frankendom-<lane>-<x> and that lane (named by the cwd's ~/Developer/frankendom-<lane>
folder) already has two side worktrees of frankendom-main. Rule text: RPG-game AGENTS.md, PR #2874.

Fail-open everywhere it cannot be sure: an unknown cwd, a path it cannot resolve (an unexpanded variable), another lane's prefix, a
missing or slow git. Quoted text (commit messages, echo) is data, not a command.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from .bash_write_guard import _split as split_segments

DEVELOPER = Path.home() / "Developer"
MAIN_REPO = DEVELOPER / "frankendom-main"
MAX_SIDE_COPIES = 2
LISTED = 5     # the deny names this many of the lane's copies (combat had 36 on 2026-10-16)
MESSAGE = "two side copies max: remove a finished one first (git worktree remove <path>), AGENTS.md"

WORKTREE_ADD_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*git\s+(?:(?:-C|-c)\s+\S+\s+)*worktree\s+add\b(.*)$", re.DOTALL)
OPTIONS_WITH_VALUE = {"-b", "-B", "--reason"}     # git worktree add [-f] [--detach] [-b <branch>] <path> [<commit-ish>]


def lane_of(cwd: str) -> str:
    """The <lane> of a cwd under ~/Developer/frankendom-<lane>, else ''."""
    try:
        rel = Path(os.path.normpath(cwd)).relative_to(DEVELOPER)
    except (ValueError, TypeError):
        return ""
    first = rel.parts[0] if rel.parts else ""
    return first[len("frankendom-"):] if first.startswith("frankendom-") and len(first) > len("frankendom-") else ""


def side_copies(prefix: str) -> list[str]:
    """Worktrees of frankendom-main that sit directly in ~/Developer and whose folder name starts with prefix."""
    out = subprocess.run(["git", "-C", str(MAIN_REPO), "worktree", "list", "--porcelain"],
                         capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        return []
    paths = [line[len("worktree "):] for line in out.stdout.splitlines() if line.startswith("worktree ")]
    return [p for p in paths if Path(p).parent == DEVELOPER and Path(p).name.startswith(prefix)]


def _target(args: str, cwd: str) -> str:
    """The absolute path a `git worktree add` creates, '' when it cannot be told."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        return ""
    skip = False
    for tok in tokens:
        if skip:
            skip = False
        elif tok in OPTIONS_WITH_VALUE:
            skip = True
        elif not tok.startswith("-"):
            path = os.path.expandvars(os.path.expanduser(tok))
            if "$" in path:
                return ""
            return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    return ""


def worktree_add_reason(command: str, cwd: str = "", listing: Callable[[str], list[str]] | None = None) -> str:
    lane = lane_of(cwd or "")
    if not lane or "worktree" not in (command or ""):
        return ""
    prefix = f"frankendom-{lane}-"
    for seg in split_segments(command):
        m = WORKTREE_ADD_RE.match(seg)
        if not m:
            continue
        raw = _target(m.group(1), cwd)
        target = Path(raw) if raw else None
        if target is None or target.parent != DEVELOPER or not target.name.startswith(prefix):
            continue
        try:
            existing = (listing or side_copies)(prefix)
        except (OSError, subprocess.SubprocessError):
            return ""
        if len(existing) >= MAX_SIDE_COPIES:
            names = sorted(existing)
            more = f" (+{len(names) - LISTED} more)" if len(names) > LISTED else ""
            return f"{MESSAGE}. Yours: {', '.join(names[:LISTED])}{more}"
    return ""

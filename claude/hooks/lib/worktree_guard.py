"""One side worktree per lane (Lead + Dom, 2026-10-16: "a universal fix so this wastage doesn't happen again"; cap set to 1 by Strategy).

The Mac reached 94% disk with 65 finished side copies (~70 GB) of ~/Developer/frankendom-main. `worktree_add_reason` refuses
`git worktree add <path>` when <path> is ~/Developer/frankendom-<lane>-<x> and that lane (named by the cwd's ~/Developer/frankendom-<lane>
folder) already has a side worktree of frankendom-main. Dom's two sandboxes and frankendom-main are not lanes for this count.
Rule text: RPG-game AGENTS.md, PR #2874.

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
MAX_SIDE_COPIES = 1    # Strategy, 2026-10-16: the lane folder plus at most one per-task side copy, removed with the PR (Lead first asked for two)
LISTED = 5     # the deny names this many of the lane's copies (combat had 36 on 2026-10-16)
MESSAGE = "remove your previous side worktree first (one per lane, Strategy 2026-10-16)"
NOT_LANES = {"sandbox", "zone-sandbox", "main"}   # Dom's two sandboxes and frankendom-main are not lanes for this count

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


# 2026-10-18 (Dom, Mac down to 23 GB free twice in a day): lanes also make ~1.1 GB worktrees in their session scratchpad,
# /private/tmp/claude-501/<lane>/<session>/scratchpad/wt-*, which the ~/Developer cap never saw (52 of them, 83 GB). Cap: 3 per scratchpad,
# the hooks lane's pick (the ~/Developer cap is 1); one constant to change. Same fail-open rule as above.
SCRATCH_ROOTS = ("/private/tmp/claude-501/", "/tmp/claude-501/")
MAX_SCRATCH_WORKTREES = 3


def scratchpad_of(path: str) -> str:
    """The scratchpad dir a path under claude-501 belongs to (<lane>/<session>/scratchpad, else the lane dir), '' elsewhere."""
    for root in SCRATCH_ROOTS:
        if path.startswith(root):
            parts = path[len(root):].split("/")
            if "scratchpad" in parts:
                return root + "/".join(parts[:parts.index("scratchpad") + 1])
            return root + parts[0] if len(parts) >= 2 else ""
    return ""


def scratch_worktrees(root: str) -> list[str]:
    """Child dirs of root that hold a .git file, i.e. the git worktrees checked out there, oldest first (by .git mtime)."""
    try:
        found = [n for n in sorted(os.listdir(root)) if os.path.isfile(os.path.join(root, n, ".git"))]
        return sorted(found, key=lambda n: os.path.getmtime(os.path.join(root, n, ".git")))
    except OSError:
        return []


def scratch_worktree_reason(command: str, cwd: str = "", listing: Callable[[str], list[str]] | None = None) -> str:
    if not cwd or "worktree" not in (command or ""):
        return ""
    for seg in split_segments(command):
        m = WORKTREE_ADD_RE.match(seg)
        if not m:
            continue
        raw = _target(m.group(1), cwd)
        pad = scratchpad_of(raw) if raw else ""
        if not pad:
            continue
        try:
            existing = (listing or scratch_worktrees)(pad)
        except (OSError, subprocess.SubprocessError):
            return ""
        if len(existing) >= MAX_SCRATCH_WORKTREES:
            more = f" (+{len(existing) - LISTED} more)" if len(existing) > LISTED else ""
            return (f"this scratchpad already has {len(existing)} worktrees ({', '.join(existing[:LISTED])}{more}), about 1.1 GB each. "
                    f"The oldest is {existing[0]}: if it is finished, remove it with `git worktree remove {pad}/{existing[0]}`, then retry. "
                    f"This guard only refuses and never deletes anything (cap {MAX_SCRATCH_WORKTREES}, Dom 2026-10-18: the Mac hit 23 GB free; Lead go #163)")
    return ""


def worktree_add_reason(command: str, cwd: str = "", listing: Callable[[str], list[str]] | None = None) -> str:
    lane = lane_of(cwd or "")
    if not lane or lane in NOT_LANES or "worktree" not in (command or ""):
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

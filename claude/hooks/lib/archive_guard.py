"""Archive cap for session scratchpads (Dom + Strategy, 2026-10-18: "3 archives max, we don't need 25 backups").

The deploy and combat scratchpads held 25 old *.tgz/*.zip bundles (17 GB) and the Mac fell to 13 GB free. `archive_reason` refuses a command that
CREATES an archive (`tar -c...`, `zip`) inside a session scratchpad, /private/tmp/claude-501/<lane>/<session>/scratchpad, when
  - that scratchpad already holds MAX_PER_PAD archives over MIN_BYTES, or
  - all scratchpads together already hold more than MAX_TOTAL_BYTES of such archives (stops 9 lanes x 3 from being 27 again).
The refusal names the oldest archives to delete. The guard only refuses: it never deletes anything. Extracting, listing, and archives written
anywhere else are not touched. Fail open everywhere it cannot be sure: an unresolved path, an unreadable directory, a slow walk.
"""
from __future__ import annotations

import glob
import os
import re
import shlex
import time
from typing import Callable

from .bash_write_guard import _split as split_segments
from .worktree_guard import SCRATCH_ROOTS, scratchpad_of

ARCHIVE_EXTS = (".tgz", ".tar.gz", ".zip")
MIN_BYTES = 100 * 1024 * 1024
MAX_PER_PAD = 3
MAX_TOTAL_BYTES = 10 * 1024 ** 3
NAMED = 3                 # the refusal names this many of the oldest archives
WALK_BUDGET_S = 3.0       # a slower walk fails open
SKIP_DIRS = {".git", "node_modules"}

TAR_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*tar\s+(.*)$", re.DOTALL)
ZIP_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*zip\s+(.*)$", re.DOTALL)

Archive = tuple[float, int, str]      # (mtime, bytes, path)


def _is_archive(path: str) -> bool:
    return path.endswith(ARCHIVE_EXTS)


def _tar_output(args: str) -> str:
    """The file a `tar -c...` writes: -f X, --file=X, --file X, or the word after a bundled flag cluster like czf. '' for anything that is not a create."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        return ""
    create = False
    out = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--create", "-c") or (tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:] and tok[1:].isalpha()):
            create = True
        if tok == "--file" and i + 1 < len(tokens):
            out = tokens[i + 1]
        elif tok.startswith("--file="):
            out = tok[len("--file="):]
        elif tok.startswith("-") and not tok.startswith("--") and tok[1:].isalpha() and tok.endswith("f") and i + 1 < len(tokens):
            out = tokens[i + 1]
        elif i == 0 and not tok.startswith("-") and tok.isalpha() and "c" in tok and tok.endswith("f") and i + 1 < len(tokens):
            create = True        # old style: tar czf out.tgz dir
            out = tokens[i + 1]
        i += 1
    return out if create else ""


def _zip_output(args: str) -> str:
    try:
        tokens = shlex.split(args)
    except ValueError:
        return ""
    for tok in tokens:
        if not tok.startswith("-"):
            return tok
    return ""


def archive_target(command: str, cwd: str) -> str:
    """The absolute path of the archive a command creates, '' when none or when it cannot be told."""
    for seg in split_segments(command or ""):
        m = TAR_RE.match(seg)
        raw = _tar_output(m.group(1)) if m else ""
        if not raw:
            z = ZIP_RE.match(seg)
            raw = _zip_output(z.group(1)) if z else ""
        if not raw or not _is_archive(raw):
            continue
        path = os.path.expandvars(os.path.expanduser(raw))
        if "$" in path:
            return ""
        return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    return ""


def _walk(root: str, deadline: float) -> list[Archive]:
    found: list[Archive] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > deadline:
            raise OSError("walk too slow")
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not os.path.isfile(os.path.join(dirpath, d, ".git"))]
        for name in filenames:
            if _is_archive(name):
                p = os.path.join(dirpath, name)
                st = os.stat(p)
                if st.st_size >= MIN_BYTES:
                    found.append((st.st_mtime, st.st_size, p))
    return sorted(found)


def pad_archives(pad: str) -> list[Archive]:
    """Archives over MIN_BYTES in one scratchpad, oldest first. [] when it is missing."""
    if not os.path.isdir(pad):
        return []
    return _walk(pad, time.monotonic() + WALK_BUDGET_S)


def all_archives() -> list[Archive]:
    deadline = time.monotonic() + WALK_BUDGET_S
    found: list[Archive] = []
    for root in SCRATCH_ROOTS:
        for pad in glob.glob(root + "*/*/scratchpad"):
            found.extend(_walk(pad, deadline))
    return sorted(set(found))


def _names(items: list[Archive]) -> str:
    return ", ".join(f"{p} ({s / 1024 ** 3:.1f} GB)" for _, s, p in items[:NAMED])


def archive_reason(command: str, cwd: str = "", pad_listing: Callable[[str], list[Archive]] | None = None,
                   all_listing: Callable[[], list[Archive]] | None = None) -> str:
    if not cwd or not any(w in (command or "") for w in ("tar", "zip")):
        return ""
    target = archive_target(command, cwd)
    pad = scratchpad_of(target) if target else ""
    if not pad or not pad.endswith("/scratchpad"):
        return ""
    try:
        mine = (pad_listing or pad_archives)(pad)
        everywhere = (all_listing or all_archives)()
    except (OSError, ValueError):
        return ""
    if len(mine) >= MAX_PER_PAD:
        return (f"this scratchpad already has {len(mine)} archives over 100 MB (cap {MAX_PER_PAD}, Dom 2026-10-18: \"3 archives max\"). "
                f"Delete the oldest first: {_names(mine)}. This guard only refuses and never deletes anything")
    if sum(s for _, s, _ in everywhere) > MAX_TOTAL_BYTES:
        total = sum(s for _, s, _ in everywhere) / 1024 ** 3
        return (f"all scratchpads together hold {total:.1f} GB of archives (cap 10 GB, Strategy 2026-10-18). "
                f"Delete the oldest first: {_names(everywhere)}. This guard only refuses and never deletes anything")
    return ""

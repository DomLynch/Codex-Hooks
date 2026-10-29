#!/usr/bin/env python3
"""Daily janitor for /private/tmp/claude-501 (lane scratch). REPORT ONLY: it lists removal candidates and deletes nothing.

Strategy's condition for merging (2026-10-29): the janitor never deletes by age on its own. A candidate is an output kind
(dist, build, node_modules, out, .vite, .cache, tmp, test-results, coverage, clips*, *.tgz/*.zip/*.tar.gz) that is
  - untouched for 24 h (newest file inside),
  - not open by any process (lsof),
  - not inside a git worktree or clone (any .git above it, up to the root),
  - not inside a scratchpad whose session is running (transcript touched in the last 24 h, or its id in `ps`),
  - not a stills folder.
It never leaves the root, never follows symlinks and never touches ~/Developer. Every candidate is logged.
Run by launchd daily (scripts/install_scratch_janitor.sh), outside any session."""

import os
import re
import subprocess
import sys
import time

ROOT = "/private/tmp/claude-501"
LOG = os.path.expanduser("~/.claude/logs/scratch_janitor.log")
PROJECTS = os.path.expanduser("~/.claude/projects")
AGE = 24 * 3600
MAX_DEPTH = 6
DIR_KINDS = {
    "dist",
    "build",
    "node_modules",
    "out",
    ".vite",
    ".cache",
    "tmp",
    "test-results",
    "coverage",
}
FILE_SUFFIXES = (".tgz", ".zip", ".tar.gz")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_kind(name, is_dir):
    if is_dir:
        return name in DIR_KINDS or name.startswith("clips")
    return name.endswith(FILE_SUFFIXES)


def newest_mtime(path):
    """Newest mtime of path or anything inside it (symlinks not followed)."""
    newest = os.lstat(path).st_mtime
    if os.path.isdir(path) and not os.path.islink(path):
        for base, dirs, files in os.walk(path):
            for n in dirs + files:
                try:
                    newest = max(newest, os.lstat(os.path.join(base, n)).st_mtime)
                except OSError:
                    pass
    return newest


def inside_git(path, root):
    p = os.path.dirname(path)
    while len(p) > len(root):
        if os.path.exists(os.path.join(p, ".git")):
            return True
        p = os.path.dirname(p)
    return False


def running_ids():
    out = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True).stdout
    return set(UUID.findall(out))


def session_active(path, root, now, ids, projects=PROJECTS):
    """True when path sits under <slug>/<session-id>/ and that session is running (id in ps, or transcript fresh)."""
    parts = os.path.relpath(path, root).split(os.sep)
    if len(parts) < 2 or not UUID.fullmatch(parts[1]):
        return False
    sid = parts[1]
    if sid in ids:
        return True
    t = os.path.join(projects, parts[0], sid + ".jsonl")
    return os.path.exists(t) and now - os.path.getmtime(t) < AGE


def is_open(path):
    try:
        r = subprocess.run(
            ["lsof", "-t", "+D", path] if os.path.isdir(path) else ["lsof", "-t", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return True  # cannot tell, so treat as in use
    return bool(r.stdout.strip())


def candidates(root=ROOT, now=None, ids=None, projects=PROJECTS, open_check=is_open):
    now = now or time.time()
    ids = running_ids() if ids is None else ids
    found = []
    depth0 = root.rstrip(os.sep).count(os.sep)
    for base, dirs, files in os.walk(root):
        depth = base.count(os.sep) - depth0
        keep = []
        for d in dirs:
            p = os.path.join(base, d)
            if os.path.islink(p):
                continue
            if "still" in d.lower():
                continue
            if is_kind(d, True):
                if (
                    not inside_git(p, root)
                    and not session_active(p, root, now, ids, projects)
                    and now - newest_mtime(p) >= AGE
                    and not open_check(p)
                ):
                    found.append(p)
                continue  # never descend into an output folder
            if depth < MAX_DEPTH:
                keep.append(d)
        dirs[:] = keep
        for f in files:
            p = os.path.join(base, f)
            if (
                is_kind(f, False)
                and not os.path.islink(p)
                and not inside_git(p, root)
                and not session_active(p, root, now, ids, projects)
                and now - os.lstat(p).st_mtime >= AGE
                and not open_check(p)
            ):
                found.append(p)
    return found


def size_of(path):
    if os.path.isfile(path):
        return os.lstat(path).st_size
    total = 0
    for base, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
    return total


def main(argv):
    """Report only (Strategy, 2026-10-29: the janitor deletes nothing by age on its own; removal goes through the archive-first vps-scratch-sweep)."""
    root = ROOT
    cands = candidates(root)
    total = 0
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as log:
        for p in cands:
            sz = size_of(p)
            total += sz
            print("candidate %.2f GB  %s" % (sz / 1e9, p))
            log.write("%s candidate %d bytes %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), sz, p))
    print("report only: %d items, %.2f GB (nothing deleted; the janitor never removes anything)" % (len(cands), total / 1e9))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

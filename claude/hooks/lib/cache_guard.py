"""Never delete or move the Playwright browser cache (Lead + Dom, 2026-10-16).

A cleanup command removed ~/Library/Caches/ms-playwright; chromium went missing and the HL2 publish stopped. `cache_delete_reason`
denies rm/rmdir/unlink/trash/mv whose target is ms-playwright (Mac or VPS), anything inside it, or a folder that contains it
(~/Library/Caches, ~/.cache, /root/.cache, ~/Library, ~, /root, /), including a glob that would match any of those. Other caches
(~/.cache/uv, ~/Library/Caches/semble ...) stay deletable. Quoted text is data; `ssh host '<command>'` is checked with /root as home.
Fail-open where it cannot be sure (an unset variable, an odd quote).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path

from .bash_write_guard import _split as split_segments

PLAYWRIGHT = [Path.home() / "Library" / "Caches" / "ms-playwright", Path.home() / ".cache" / "ms-playwright",
              Path("/root/.cache/ms-playwright")]
ANCESTORS = {str(parent) for pw in PLAYWRIGHT for parent in pw.parents}
PROTECTED = {str(pw) for pw in PLAYWRIGHT}
DELETERS = {"rm", "rmdir", "unlink", "trash"}
WRAPPERS = {"sudo", "command", "env", "nohup", "time"}
REMOTE_RE = re.compile(r"\b(?:rm|rmdir|unlink|trash|mv)\b")
MESSAGE = ("Never delete or move ms-playwright, or a folder that contains it (Lead + Dom 2026-10-16: it vanished and stopped "
           "the HL2 publish). Name a sibling you checked no lane depends on instead, e.g. ~/Library/Caches/semble.")


def _expand(arg: str, home: str, cwd: str) -> str:
    """The absolute path an argument names, or '' when it cannot be known (unset variable, no cwd)."""
    path = arg.replace("${HOME}", home).replace("$HOME", home)
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    if "$" in path or "`" in path:
        return ""
    if not path.startswith("/"):
        if not cwd:
            return ""
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _hits(path: str) -> bool:
    if any(ch in path for ch in "*?["):
        return any(fnmatch.fnmatchcase(p, path) for p in PROTECTED | ANCESTORS)
    return path in PROTECTED or path in ANCESTORS or any(path.startswith(p + "/") for p in PROTECTED)


def _targets(words: list[str]) -> list[str]:
    out, only_files = [], False
    for word in words:
        if word == "--" and not only_files:
            only_files = True
        elif only_files or not word.startswith("-"):
            out.append(word)
    return out


def cache_delete_reason(command: str, cwd: str = "", home: str | None = None) -> str:
    here = home or str(Path.home())
    cwd = cwd or ""
    for segment in split_segments(command or ""):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        while words and (re.fullmatch(r"\w+=\S*", words[0]) or words[0] in WRAPPERS):
            words = words[1:]
        if not words:
            continue
        head, rest = os.path.basename(words[0]), words[1:]
        if head == "cd" and rest:
            cwd = _expand(rest[0], here, cwd) or cwd
        elif head == "ssh":
            for arg in rest:
                if REMOTE_RE.search(arg) and cache_delete_reason(arg, "", "/root"):
                    return MESSAGE
        elif head in DELETERS or head == "mv":
            targets = _targets(rest)
            if head == "mv":
                targets = targets[:-1]   # the last word is where it goes, not what leaves
            for target in targets:
                path = _expand(target, here, cwd)
                if path and _hits(path):
                    return MESSAGE
    return ""

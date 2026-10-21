"""Never delete or move the Playwright browser cache (Lead + Dom, 2026-10-16).

A cleanup command removed ~/Library/Caches/ms-playwright; chromium went missing and the HL2 publish stopped. `cache_delete_reason`
denies rm/rmdir/unlink/trash/mv (and `rsync --delete`, judged by its destination) whose target is ms-playwright (Mac or VPS), anything
inside it, or a folder that contains it (~/Library/Caches, ~/.cache, /root/.cache, ~/Library, ~, /root, /), including a glob that would
match any of those. Other caches (~/.cache/uv, ~/Library/Caches/semble ...) stay deletable. Quoted text is data; `ssh host '<command>'`
is checked with /root as home.

2026-10-21 (Strategy + Dom): FAIL CLOSED on a path it cannot resolve. A loop deleted ms-playwright through `rm -rf "${s:?}"` because the old
guard skipped every target containing `$`. Now an inline literal assignment (`s=~/x`) or `for s in a b c` in the same command is resolved,
and a variable or command substitution that stays unknown becomes `*`: denied when `*` could be the cache or a folder holding it
(`"${s:?}"`, `$(mktemp -d)`), allowed when a literal part rules it out (`"$DIR/node_modules"`). Still fail-open only on an odd quote.
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
KEYWORDS = {"do", "then", "if", "else", "elif", "while", "until", "!", "{"}   # shell words that can start a segment before the command
REMOTE_RE = re.compile(r"\b(?:rm|rmdir|unlink|trash|mv|rsync)\b")
VAR_RE = re.compile(r"\$\{(\w+)[^}]*\}|\$(\w+)")
SUBST_RE = re.compile(r"\$\([^)]*\)?|`[^`]*`?|\$")
ASSIGN_RE = re.compile(r"(\w+)=(.*)", re.DOTALL)
MESSAGE = ("Never delete or move ms-playwright, or a folder that contains it (Lead + Dom 2026-10-16: it vanished and stopped "
           "the HL2 publish). Name a sibling you checked no lane depends on instead, e.g. ~/Library/Caches/semble.")
UNRESOLVED = ("cache_guard: unresolvable path, use the literal path. A variable or command substitution here could name ms-playwright or a "
              "folder that holds it (Strategy + Dom 2026-10-21: a loop deleted it through $s). Write the literal path, or assign it in the "
              "same command (s=~/Library/Caches/semble).")


def _resolve(arg: str, home: str, cwd: str, env: dict[str, list[str]]) -> list[tuple[str, bool]]:
    """Every absolute path an argument can name, as (path, unknown). `unknown` means a variable or substitution we cannot resolve was
    replaced by '*' (so the path is a pattern). Empty when a relative path has no cwd to be placed in."""
    path = arg
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    options = [(path, False)]
    for _ in range(8):
        grown, changed = [], False
        for text, unknown in options:
            m = VAR_RE.search(text)
            if not m:
                grown.append((text, unknown))
                continue
            changed = True
            name = m.group(1) or m.group(2)
            values = [home] if name == "HOME" else env.get(name)
            if values is None:
                grown.append((text[:m.start()] + "*" + text[m.end():], True))
            else:
                grown.extend((text[:m.start()] + value + text[m.end():], unknown) for value in values)
        options = grown
        if not changed:
            break
    out = []
    for text, unknown in options:
        replaced = SUBST_RE.sub("*", text)
        unknown = unknown or replaced != text
        if not replaced.startswith("/") and not replaced.startswith("*"):
            if not cwd:
                continue
            replaced = os.path.join(cwd, replaced)
        out.append((os.path.normpath(replaced), unknown))
    return out


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


def _known(resolved: list[tuple[str, bool]]) -> list[str]:
    return [path for path, unknown in resolved if not unknown]


def _scan(command: str, cwd: str, here: str, env: dict[str, list[str]]) -> str:
    for segment in split_segments(command or ""):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        assigns = []
        while words and (words[0] in KEYWORDS or words[0] in WRAPPERS or re.fullmatch(r"\w+=\S*", words[0])):
            if words[0] not in KEYWORDS and words[0] not in WRAPPERS:
                assigns.append(words[0])
            words = words[1:]
        if not words:
            for assign in assigns:   # a bare `name=value` sets it for the rest of the command
                name, value = ASSIGN_RE.fullmatch(assign).groups()
                resolved = _resolve(value, here, cwd, env)
                if resolved and len(_known(resolved)) == len(resolved):
                    env[name] = _known(resolved)
                else:
                    env.pop(name, None)
            continue
        head, rest = os.path.basename(words[0]), words[1:]
        if head == "for" and len(rest) >= 3 and rest[1] == "in":
            values = [r for value in rest[2:] for r in _resolve(value, here, cwd, env)]
            if values and all(not unknown for _, unknown in values):
                env[rest[0]] = [path for path, _ in values]
            else:
                env.pop(rest[0], None)
        elif head == "cd" and rest:
            placed = _resolve(rest[0], here, cwd, env)
            if placed and not placed[0][1]:
                cwd = placed[0][0]
        elif head == "ssh":
            for arg in rest:
                if REMOTE_RE.search(arg):
                    reason = _scan(arg, "", "/root", dict(env))
                    if reason:
                        return reason
        elif head in DELETERS or head == "mv" or (head == "rsync" and any(w.startswith("--delete") for w in rest)):
            targets = _targets(rest)
            if head == "mv":
                targets = targets[:-1]   # the last word is where it goes, not what leaves
            elif head == "rsync":
                targets = targets[-1:]   # --delete empties the destination
            for target in targets:
                home = here
                if head == "rsync" and re.match(r"^[^/\s:]+:", target):
                    target, home = re.sub(r"^[^/\s:]+:", "", target), "/root"   # host:path is judged on the remote
                for path, unknown in _resolve(target, home, cwd, env):
                    if _hits(path):
                        return UNRESOLVED if unknown else MESSAGE
    return ""


def cache_delete_reason(command: str, cwd: str = "", home: str | None = None) -> str:
    return _scan(command, cwd or "", home or str(Path.home()), {})

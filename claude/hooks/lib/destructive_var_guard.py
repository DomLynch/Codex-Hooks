"""A destructive command on a variable path is a hard deny, never a popup for Dom (Dom via Strategy, 2026-10-21).

Claude Code raises its own "Allow Claude to run?" prompt when no hook decides on `rm -f $T/*`; an OpenTrident sub-agent's
`ssh root@49.12.7.18 'T=_scratch/t; rm -f $T/*'` put that prompt in front of Dom ("the hooks should be doing that"). `variable_delete_reason`
returns a one-line reason for rm/rmdir/unlink/trash/mv, `rsync --delete`, `git clean` and `find -delete/-exec rm` when a path operand holds a
variable, a command substitution or a backtick, including inside `ssh host '<command>'`, `bash -c '<command>'` and `eval`. The lane rewrites the
command with a literal path. `$HOME` and `~` stay allowed (cache_guard judges them); a heredoc body is data unless a shell reads it; quotes do
not hide a variable (the shell expands it inside double quotes, and a single-quoted `$` is rare enough to rewrite). Not covered: `xargs rm`,
whose stdin cache_guard already judges, and scripts run as files.
"""
from __future__ import annotations

import os
import re
import shlex

from .bash_write_guard import _split as split_segments
from .cache_guard import DELETERS, KEYWORDS, SHELLS, WRAPPERS, _skip_wrapper_args, _targets

REASON = ("destructive_var_guard: rm/mv/rsync --delete/git clean/find -delete on a variable, $(...) or `...` path cannot be checked and raises a "
          "permission popup for Dom. Rewrite it with the literal path (Dom 2026-10-21).")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
HOME_RE = re.compile(r"\$\{HOME\}|\$HOME\b")
SHELL_STDIN_RE = re.compile(r"(?:^|[\s;&|(])(?:ba|z|da|k)?sh(?:\s+-\w+)*\s*$")   # `bash <<EOF`: a shell reads the body
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}


def _dynamic(word: str) -> bool:
    return bool(re.search(r"\$|`", HOME_RE.sub("", word)))


def _without_heredoc_data(command: str) -> str:
    def keep(match: re.Match) -> str:
        return match.group(0) if SHELL_STDIN_RE.search(command[:match.start()].split("\n")[-1].rstrip()) else "\n"
    return HEREDOC_RE.sub(keep, command)


def _operands_dynamic(words: list[str]) -> bool:
    return any(_dynamic(word) for word in words)


def _find_dynamic(rest: list[str]) -> bool:
    deletes = "-delete" in rest or any(w in FIND_EXEC and i + 1 < len(rest) and os.path.basename(rest[i + 1]) in DELETERS | {"mv"} for i, w in enumerate(rest))
    if not deletes:
        return False
    i = 0
    while i < len(rest) and rest[i] in {"-H", "-L", "-P"}:
        i += 1
    starts = []
    while i < len(rest) and not rest[i].startswith(("-", "(", "!")):
        starts.append(rest[i])
        i += 1
    return _operands_dynamic(starts)


def _scan(command: str, depth: int = 0) -> str:
    if depth > 4:
        return ""
    text = re.sub(r"`([^`]*)`", r"$(\1)", _without_heredoc_data(command or ""))   # the segment splitter breaks on a backtick; $( ) keeps the substitution in its word
    for segment in split_segments(text):
        try:
            words = shlex.split(segment.rstrip().removesuffix("\\"))
        except ValueError:
            words = segment.split()
        while words and (words[0] in KEYWORDS or words[0] in WRAPPERS or re.fullmatch(r"\w+=\S*", words[0])):
            word, words = words[0], words[1:]
            if word in WRAPPERS:
                words = _skip_wrapper_args(word, words)
        if not words:
            continue
        head, rest = os.path.basename(words[0]), words[1:]
        if head == "eval":
            reason = _scan(" ".join(rest), depth + 1)
            if reason:
                return reason
        elif head == "ssh" or head in SHELLS:
            for arg in rest:
                if re.search(r"[\s;&|]", arg):
                    reason = _scan(arg, depth + 1)
                    if reason:
                        return reason
        elif head in DELETERS or head == "mv":
            if _operands_dynamic(_targets(rest)):
                return REASON
        elif head == "rsync" and any(w.startswith("--delete") for w in rest):
            if _operands_dynamic(_targets(rest)[-1:]):   # --delete empties the destination, the last word; `-e "ssh -i $K"` and the source are not what is deleted
                return REASON
        elif head == "git":
            args = [w for w in rest if not w.startswith("-")]
            if args[:1] == ["clean"] and _operands_dynamic(args[1:]):
                return REASON
        elif head == "find" and _find_dynamic(rest):
            return REASON
    return ""


def variable_delete_reason(command: str) -> str:
    return _scan(command)

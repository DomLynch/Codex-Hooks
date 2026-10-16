"""Bash must not write files (Dom, 2026-10-16, via Strategy: "we said the devs wouldn't use bash").

A lane's card showed a python3 heredoc rewriting a script beside `rm -f $D/*`; files are edited with Read/Edit/Write, which the app can check
and the user can review. `bash_write_reason` returns the deny reason for a Bash command that writes a file through

  - a heredoc into `cat > FILE` / `cat >> FILE` / `cat <<EOF > FILE` or `tee FILE`,
  - an inline interpreter (`python -c`, `python - <<`, `node -e`, `perl -e`, `ruby -e`) whose code opens a path for writing,
  - `sed -i` / `perl -pi` (in-place edits),
  - `echo` / `printf` redirected into a file.

Heredocs that only feed a command's stdin (ssh, psql, `gh api --input -`, `git commit -m "$(cat <<EOF ...)"`) and quoted text (commit messages,
grep patterns, a script sent over ssh) are data, not writes. A bash/sh/zsh heredoc body is judged recursively. Redirects to /dev/* and fd
redirects (`2>&1`, `>&2`) never count.
"""
from __future__ import annotations

import re

REASON = "edit files with Read/Edit/Write, not Bash (Dom, 2026-10-16)"

QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
# <<TAG, the rest of its line (redirects and pipes after the tag), the body, the terminator line.
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
SEPARATORS = ";&|()`\n"
SEGMENT_RE = re.compile(r"[;&|()`\n]+")
# Separators inside quotes are data: swap them for private-use characters so a split cannot cut a quoted script in half.
_PROTECT = {ord(c): chr(0xE000 + i) for i, c in enumerate(SEPARATORS)}
_RESTORE = {0xE000 + i: c for i, c in enumerate(SEPARATORS)}

_START = r"(?:^|[\s;&|(/])"
REDIRECT = r"(?<![0-9&])>>?\s*(?!&|/dev/)[^\s>&|]"   # a stdout redirect into a path (not fd, not /dev/*)
CAT_FILE_RE = re.compile(rf"{_START}cat\b[^\n]*?{REDIRECT}")
TEE_FILE_RE = re.compile(rf"{_START}tee\s+(?:-\w+\s+)*(?!/dev/)[^\s-]")
ECHO_REDIRECT_RE = re.compile(rf"^\s*(?:\w+=\S*\s+)*(?:echo|printf)\b[^\n]*?{REDIRECT}")
SHELL_RE = re.compile(rf"{_START}(?:ba|z)?sh\b")
INTERPRETER_RE = re.compile(rf"{_START}(?:python[\d.]*|node|perl|ruby)\b")
INLINE_CODE_RE = re.compile(rf"{_START}(?:python[\d.]*|node|perl|ruby)\s+(?:-\S+\s+)*?-[A-Za-z]*[ce](?=\s|$)")
IN_PLACE_RE = re.compile(rf"{_START}sed\s+(?:-\S+\s+)*?(?:-[A-Za-z]*i\S*|--in-place\S*)(?=\s|$)|{_START}perl\s+(?:-\S+\s+)*?-[A-Za-z]*i\S*(?=\s|$)")
# What interpreter code does when it writes: python open(.., 'w'|'a'|'x'|'+'), write_text/write_bytes, copies and moves; node fs writes;
# perl open(F, ">f"); ruby File.write / File.open(.., 'w').
WRITE_CODE_RE = re.compile(
    r"open\s*\([^()]*?,\s*(?:mode\s*=\s*)?['\"][rwxabt+]*[wax+][rwxabt+]*['\"]"
    r"|\.write_(?:text|bytes)\s*\(|shutil\.(?:copy\w*|move)\s*\(|os\.(?:replace|rename)\s*\("
    r"|\b(?:writeFile|appendFile|copyFile)(?:Sync)?\s*\(|createWriteStream\s*\(|renameSync\s*\("
    r"|open\s*\(?\s*\w+\s*,\s*['\"]?\s*[>+]"
    r"|File\.(?:write|open\([^()]*['\"][wa])|IO\.write\s*\("
)


def _flat(text: str) -> str:
    """Quoted strings reduced to "" so redirects and command words inside them are not seen."""
    return QUOTED_RE.sub('""', text)


def _split(text: str) -> list[str]:
    protected = QUOTED_RE.sub(lambda m: m.group(0).translate(_PROTECT), text)
    return [seg.translate(_RESTORE) for seg in SEGMENT_RE.split(protected) if seg.strip()]


def _consumer(command: str, m: re.Match) -> str:
    """The command a heredoc feeds: the last segment before `<<`, plus whatever follows the tag on that line."""
    protected = QUOTED_RE.sub(lambda q: q.group(0).translate(_PROTECT), command[:m.start()])
    last = SEGMENT_RE.split(protected)[-1].translate(_RESTORE)
    return _flat(last + " " + m.group(3))


def _heredoc_writes(consumer: str, body: str, depth: int) -> bool:
    if CAT_FILE_RE.search(consumer) or TEE_FILE_RE.search(consumer):
        return True
    if SHELL_RE.search(consumer):
        return depth < 3 and bash_write_reason(body, depth + 1) != ""
    if INTERPRETER_RE.search(consumer):
        return WRITE_CODE_RE.search(body) is not None
    return False


def bash_write_reason(command: str, depth: int = 0) -> str:
    command = command or ""
    pieces: list[str] = []
    last = 0
    has_heredoc = False
    for m in HEREDOC_RE.finditer(command):
        has_heredoc = True
        if _heredoc_writes(_consumer(command, m), m.group(4), depth):
            return REASON
        pieces.append(command[last:m.start()] + " <<HD" + m.group(3))
        last = m.end()
    pieces.append(command[last:])
    for raw in _split("".join(pieces)):
        masked = _flat(raw)
        if ECHO_REDIRECT_RE.search(masked) or IN_PLACE_RE.search(masked):
            return REASON
        if has_heredoc and TEE_FILE_RE.search(masked):
            return REASON
        if INLINE_CODE_RE.search(masked) and WRITE_CODE_RE.search(raw):
            return REASON
    return ""

"""A Hugging Face token never reaches a command line, commit, PR body or message (Dom via Strategy, 2026-10-21).

Dom put an ACCOUNT-WIDE token on the VPS at /root/.hf/token and ruled that Hugging Face jobs run from the VPS. Only a job script may read it
(`HF_TOKEN=$(cat /root/.hf/token)` inside the script); it is never echoed, never on a command line, log, PR body, receipt or message.

THE RULE (Auditor recheck, 2026-10-21, after v1 matched the wording `cat /root/.hf/token` and missed globs, `to''ken`, `F=/root/.hf; cat $F/token`,
`cd /root/.hf && cat token`, `python3 -c open(...)`, `tar`/`scp` of the folder and `echo $HF_TOKEN`): fail CLOSED on any Bash command whose
RESOLVED arguments touch /root/.hf (or ~/.hf) or print the token variable. A token split across strings cannot be caught by a regex, so the
guard closes the read instead. `hf_token_reason` returns the deny reason for a command that
  - contains a token-shaped string (`hf_` + 34 letters or digits) anywhere,
  - has an argument that resolves (shell quotes removed, `~`, `$HOME`, `cd`, an inline `F=...` or `for` assignment, and a shell glob, where a
    `*` does not match a dot-folder) to the token folder or a file inside it,
  - has an argument that resolves to a PARENT of the folder (`/root`, `/`) while the command copies or archives (tar, zip, scp, rsync, cp, mv ...),
  - prints the token variable ($HF_TOKEN, $HUGGING_FACE_HUB_TOKEN ...) on a command line,
  - runs `python -c` / `node -e` style code that names `.hf`, or does any of the above inside `ssh host '<command>'` / `bash -c '<command>'`.
Exceptions: ls, stat and test (they read no content), and the text of a message flag (-m, --body, --title ...), which is data.
Scope on purpose: Bash command lines only (a job script's own body, Write/Edit content and non-Bash tools are out of scope). The reason never
repeats the token.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path

from .bash_write_guard import _split as split_segments
from .cache_guard import ASSIGN_RE, KEYWORDS, WRAPPERS, _resolve

TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{34}")
HF_ROOTS = sorted({"/root/.hf", str(Path.home() / ".hf")})
TOKEN_VARS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN")
VAR_WORD = re.compile(r"\$\{?(?:" + "|".join(TOKEN_VARS) + r")\b|^(?:" + "|".join(TOKEN_VARS) + r")$")
CODE_HF = re.compile(r"(?<![\w])\.hf(?![\w])")
SAFE_HEADS = {"ls", "stat", "test", "[", "[["}
SHELL_HEADS = {"ssh", "bash", "sh", "zsh"}
CODE_HEADS = {"node", "perl", "ruby", "php", "deno", "bun", "awk", "gawk", "lua"}
COPIERS = {"tar", "zip", "7z", "7za", "scp", "rsync", "cp", "mv", "ditto", "cpio", "pax", "gzip", "bzip2", "xz", "zstd"}
MESSAGE_FLAGS = {"-m", "--message", "--body", "-b", "--title", "-t", "--notes", "--subject"}
MESSAGE_PREFIXES = tuple(f"{flag}=" for flag in MESSAGE_FLAGS if flag.startswith("--"))
MESSAGE = ("A Hugging Face token never goes on a command line, in a commit, PR body or message, and nothing outside a job script touches "
           "/root/.hf (Dom 2026-10-21). Only a job script reads it: HF_TOKEN=$(cat /root/.hf/token) inside the script, never echoed or "
           "copied. Run the job from the VPS and read the result file instead.")


def _component_match(pattern: str, target: str) -> bool:
    if target.startswith(".") and not pattern.startswith("."):
        return False   # a shell * or ? never matches a leading dot
    return fnmatch.fnmatchcase(target, pattern)


def _touches(path: str, copier: bool) -> bool:
    parts = [""] if path == "/" else path.rstrip("/").split("/")
    for root in HF_ROOTS:
        target = root.split("/")
        paired = list(zip(parts, target))
        if not all(_component_match(p, t) for p, t in paired):
            continue
        if len(parts) >= len(target) or copier:   # the folder itself or inside it; a parent only when the command copies or archives
            return True
    return False


def hf_token_reason(command: str, cwd: str = "", home: str | None = None, _depth: int = 0, _env: dict | None = None) -> str:
    command = command or ""
    if TOKEN_RE.search(command):
        return MESSAGE
    here = home or str(Path.home())
    env = {} if _env is None else _env
    for segment in split_segments(command):
        try:
            words = shlex.split(segment)
        except ValueError:   # cannot be read: close it only when the segment names the folder or the variable
            if ".hf" in segment or any(var in segment for var in TOKEN_VARS):
                return MESSAGE
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
                if resolved and all(not unknown for _, unknown in resolved):
                    env[name] = [path for path, _ in resolved]
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
            continue
        if head == "cd" and rest:
            placed = _resolve(rest[0], here, cwd, env)
            if placed and not placed[0][1]:
                cwd = placed[0][0]
            continue
        if head in SAFE_HEADS:
            continue
        if head in SHELL_HEADS and _depth < 3:
            for arg in rest:
                if " " in arg and hf_token_reason(arg, "", "/root", _depth + 1, dict(env)):
                    return MESSAGE
        copier = head in COPIERS
        code = head.startswith("python") or head in CODE_HEADS
        skip = False
        for word in rest:
            if skip:
                skip = False
                continue
            if word in MESSAGE_FLAGS:
                skip = True
                continue
            if word.startswith(MESSAGE_PREFIXES) or (word.startswith("-m") and len(word) > 2 and not word.startswith("--")):
                continue
            if VAR_WORD.search(word) or (code and CODE_HF.search(word)):
                return MESSAGE
            candidates = [word, re.sub(r"^-{1,2}[\w-]+=", "", word)]   # --opt=/path
            if head in {"scp", "rsync"}:
                candidates.append(re.sub(r"^[^/\s:]+:", "", word))      # host:/path names a path on the remote
            for candidate in candidates:
                if any(_touches(path, copier) for path, _unknown in _resolve(candidate, "/root" if candidate != word else here, cwd, env)):
                    return MESSAGE
    return ""

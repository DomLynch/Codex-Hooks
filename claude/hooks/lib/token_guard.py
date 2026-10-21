"""A Hugging Face token never reaches a command line, commit, PR body or message (Dom via Strategy, 2026-10-21).

Dom put an ACCOUNT-WIDE token on the VPS at /root/.hf/token and ruled that Hugging Face jobs run from the VPS. Only a job script may read it
(`HF_TOKEN=$(cat /root/.hf/token)` inside the script); it is never echoed, never on a command line, log, PR body, receipt or message.

`hf_token_reason` returns a deny reason for a Bash command that
  - contains a token-shaped string (`hf_` + 34 letters or digits), anywhere, quoted or not (a commit message, `gh pr create --body`, a header), or
  - names the token file as an argument of anything but ls/stat/test (cat, head, base64, cp, scp, `$(cat ...)`, a redirect), also inside
    `ssh host '<command>'` and `bash -c '<command>'`.
Mentioning the path inside a longer quoted sentence is data, not a read. The reason never repeats the token.
Narrow on purpose: it checks Bash command lines only (a script's own body, Write/Edit content and non-Bash tools are out of scope), and
fails open where the command cannot be parsed and does not name the token file.
"""
from __future__ import annotations

import re
import shlex

from .bash_write_guard import _split as split_segments

TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{34}")
PATH_HINT = ".hf/token"
# a word that IS the token file: /root/.hf/token, ~/.hf/token, $HOME/.hf/token, .hf/token, host:/root/.hf/token, <file, --opt=file
PATH_WORD = re.compile(r"(?:^|[=:<>])(?:/root|~|\$HOME|\$\{HOME\}|/Users/[^/\s]+)?/?\.hf/token/?$")
SAFE_HEADS = {"ls", "stat", "test", "[", "[["}
SHELL_HEADS = {"ssh", "bash", "sh", "zsh"}
WRAPPERS = {"sudo", "command", "env", "nohup", "time"}
MESSAGE = ("A Hugging Face token never goes on a command line, in a commit, PR body or message (Dom 2026-10-21). Only a job script reads "
           "/root/.hf/token: HF_TOKEN=$(cat /root/.hf/token) inside the script, never echoed or copied. Run the job from the VPS and "
           "read the result file instead.")


def _file_reason(command: str, depth: int = 0) -> str:
    for segment in split_segments(command):
        if PATH_HINT not in segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return MESSAGE   # names the token file and cannot be read: the secret wins over the convenience
        while words and (re.fullmatch(r"\w+=\S*", words[0]) or words[0] in WRAPPERS):
            words = words[1:]
        if not words:
            continue
        head, rest = words[0].rsplit("/", 1)[-1], words[1:]
        if head in SAFE_HEADS:
            continue
        if any(PATH_WORD.search(word) for word in words):
            return MESSAGE
        if head in SHELL_HEADS and depth < 3:
            for word in rest:
                if PATH_HINT in word and " " in word and _file_reason(word, depth + 1):
                    return MESSAGE
    return ""


def hf_token_reason(command: str) -> str:
    command = command or ""
    if TOKEN_RE.search(command):
        return MESSAGE
    return _file_reason(command) if PATH_HINT in command else ""

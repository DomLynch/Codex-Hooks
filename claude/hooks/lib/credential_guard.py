"""No printing of credential files, token strings or the environment (Physics Dev + Dom, 2026-10-29).

On 2026-10-28 an agent ran a `sed` meant to mask secrets over /root/.git-credentials on the VPS and printed a ghp_ token into its tool output, and so into the session transcript. The
Hugging Face token already had token_guard (Dom 2026-10-21); this extends the same never-echo rule to GitHub tokens and the other credential files. Bash output cannot be redacted after
the fact, so the guard closes the read.

`credential_reason` refuses a Bash command that
  - READS a credential file with anything but an existence check: /root|~/.git-credentials, .netrc, .config/gh/hosts.yml, .aws/credentials, a private ssh key (~/.ssh/id_* without .pub), also
    through ssh / bash -c (the command text still names the file), `< file` redirects and `python -c open(...)`,
  - carries a GitHub token-shaped string (ghp_, gho_, ghu_, ghs_, ghr_ + 36 characters, or github_pat_ + 22 or more),
  - dumps the environment (bare `env`, `printenv`, `export -p`, `declare -p`, `set`, or `env | ...`) or prints a token variable ($GH_TOKEN, $GITHUB_TOKEN ...).
Passes: ls, stat, test, wc, rm, git credential-store erase/ssh-keygen -l (they reveal no content), a `.pub` key, `env VAR=x cmd`, `env -i`, `printenv HOME`, and quoted message text (echo, a commit
message), which is data. The command is matched as text, not parsed: a miss lets the command through, a hit never touches a file. The reason never repeats a secret.
"""
from __future__ import annotations

import re

MESSAGE = ("A credential never gets printed (Physics Dev + Dom 2026-10-29: a sed over /root/.git-credentials leaked a GitHub token into a transcript on 2026-10-28). "
           "Credential files (.git-credentials, .netrc, gh hosts.yml, .aws/credentials, private ssh keys), token-shaped strings and environment dumps are refused. "
           "Check a login with `gh auth status` or a status code (curl -s -o /dev/null -w '%{http_code}' ... from a script), never by reading the file; "
           "to remove a dead entry, write a script that never prints the line.")

CRED_FILE = r"""(?:\.git-credentials|\.netrc|\.config/gh/hosts\.yml|\.aws/credentials|\.ssh/id_(?:rsa|ed25519|ecdsa|dsa)(?!\.pub)(?![\w.-]))"""
# A reader command, then (same segment) an argument that names a credential file; or a `<` redirect of one; or python/node open().
READERS = r"""(?:cat|bat|less|more|head|tail|sed|awk|gawk|grep|egrep|fgrep|rg|ag|cut|sort|uniq|tr|nl|od|xxd|hexdump|strings|base64|openssl|cp|mv|scp|rsync|ditto|tee|diff|cmp|paste|column|fold|rev|tac|zcat|xargs|source|\.)"""
READ_CMD = re.compile(rf"""(?:^|[;&|(`\n]|\$\()\s*(?:\w+=\S*\s+)*(?:sudo\s+)?{READERS}\b[^;&|\n]*?{CRED_FILE}""")
REDIRECT_IN = re.compile(rf"""<\s*["']?[^\s;&|"']*{CRED_FILE}""")
CODE_OPEN = re.compile(rf"""(?:python3?|node|ruby|perl|php)\b[^\n]*{CRED_FILE}""")
SSH_PAYLOAD = re.compile(rf"""\bssh\b[^\n]*\b{READERS}\b[^\n]*?{CRED_FILE}""")
SCP_REMOTE = re.compile(rf"""\b(?:scp|rsync)\b[^\n]*:[^\s]*{CRED_FILE}""")
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})")
ENV_DUMP = re.compile(r"""(?:^|[;&|(`\n]|\$\()\s*(?:sudo\s+)?(?:env|printenv|set|export\s+-p|declare\s+-[px]+|typeset\s+-x)\s*(?:$|[;&|)`\n])""")
ENV_IN_PAYLOAD = re.compile(r"""\bssh\b[^\n]*['"]\s*(?:env|printenv|set|export\s+-p|declare\s+-p)\s*(?:$|[;&|'"\s])""")
SECRET_VARS = re.compile(r"""\$\{?(?:GH_TOKEN|GITHUB_TOKEN|GITHUB_PAT|GH_ENTERPRISE_TOKEN|NPM_TOKEN|AWS_SECRET_ACCESS_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY)\b|\bprintenv\s+(?:GH_TOKEN|GITHUB_TOKEN|GITHUB_PAT|NPM_TOKEN|AWS_SECRET_ACCESS_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY)\b""")
WC_COUNT = re.compile(r"""\bwc(?:\s+-\w+)*\s*<\s*["']?\S+""")   # `wc -l < file` counts lines and shows no content (replay of 1141 real commands: the only hit)
ECHO_ONLY = re.compile(r"^\s*(?:echo|printf)\b")
QUOTED_MESSAGE = re.compile(r"""(?:-m|--message|--body|-b|--title|--notes)(?:\s+|=)(?:"[^"]*"|'[^']*')""")


def credential_reason(command: str) -> str:
    text = command or ""
    if not text.strip():
        return ""
    if TOKEN_RE.search(text):
        return MESSAGE
    if ECHO_ONLY.match(text) and not SECRET_VARS.search(text):
        return ""
    bare = WC_COUNT.sub(" ", QUOTED_MESSAGE.sub(" ", text))
    if READ_CMD.search(bare) or REDIRECT_IN.search(bare) or CODE_OPEN.search(bare) or SSH_PAYLOAD.search(bare) or SCP_REMOTE.search(bare):
        return MESSAGE
    if ENV_DUMP.search(bare) or ENV_IN_PAYLOAD.search(bare) or SECRET_VARS.search(bare):
        return MESSAGE
    return ""

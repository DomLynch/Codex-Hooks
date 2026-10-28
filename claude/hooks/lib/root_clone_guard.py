"""No git clone / worktree add into /root (Dom + Strategy, 2026-10-28).

The VPS root disk hit 98% twice in one day. /root held 70 GB of per-task clones (f3v-*, bw-*, boss-*, v3-*, arma-*) that vps-scratch-sweep cannot sweep, because /root is not
one of its roots. Strategy's ruling: agents clone into /srv/dev-jobs/<lane>/<name>, one reusable clone per lane, deleted on merge, where the sweep archives them.

`root_clone_reason` refuses a `git clone` or `git worktree add` whose destination is under /root, including one sent through ssh (the command text still holds it), and
`cd /root && git clone <url>` with no destination. It only refuses and never deletes anything. Quoted echo text and reads of /root (ls, cat, du, git -C /root/x pull)
pass. The command is matched as text, not parsed: a miss lets the command through, a hit never touches a file.
"""
from __future__ import annotations

import re

MESSAGE = ("a git clone / worktree add into /root fills the VPS root disk and vps-scratch-sweep cannot sweep /root (it hit 98% twice on 2026-10-28, /root was 70 GB). "
           "Clone into /srv/dev-jobs/<lane>/<name> instead, one reusable clone per lane, deleted when its PR merges (Dom + Strategy 2026-10-28)")

# `git [-C x] clone ...` / `git [-C x] worktree add ...` up to the end of that command (; & | newline or a closing quote), then a /root/<name> argument in it.
CLONE_TO_ROOT = re.compile(r"""\bgit\s+(?:-C\s+\S+\s+)?(?:clone|worktree\s+add)\b[^;&|\n'"]*?\s(?:~root|/root)/[^\s;&|'"]+""")
# `cd /root` then a clone in the same command line: the destination is /root/<repo> or /root/<given name>.
CD_ROOT_THEN_CLONE = re.compile(r"""\bcd\s+/root/?\s*(?:&&|;)\s*git\s+(?:-C\s+\S+\s+)?(?:clone|worktree\s+add)\b""")
ECHO_ONLY = re.compile(r"^\s*(?:echo|printf)\b")


def root_clone_reason(command: str) -> str:
    text = command or ""
    if "/root" not in text or "git" not in text or ECHO_ONLY.match(text):
        return ""
    if CLONE_TO_ROOT.search(text) or CD_ROOT_THEN_CLONE.search(text):
        return MESSAGE
    return ""

"""No heavy suite / build / browser / Blender run over ssh (Dom + Strategy, 2026-10-29).

The VPS hit load 68 from lane suites and builds. Dom's ruling: all lanes' suites and builds move to Hugging Face CPU, the VPS is for live sites only. Lanes kept forgetting
`hfjob` because nothing enforced it, so this guard refuses `ssh ... <heavy command>` on the Mac with "use hfjob -- <cmd>". Allow-list: deploys (scripts/deploy.sh), the sweep
tool (vps-scratch-sweep) and hfjob itself are not heavy shapes, so they pass; so do reads (df, uptime, ls, tail, git pull).

`ssh_heavy_reason` finds an `ssh` that is the command word of a segment, then judges the remote text (quoted payload included) segment by segment: a heavy command must be the
COMMAND of a payload segment (after env assignments / nice / time / `capture --prio N lane`), so `echo npm test` or a commit message naming it passes. The text is matched,
not parsed: a miss lets the command through, a hit never runs or deletes anything.
"""
from __future__ import annotations

import re

MESSAGE = ("Suites, builds, browsers and Blender never run on the VPS: it is for live sites only (load hit 68 on 2026-10-28; Dom + Strategy 2026-10-29, 'all lanes' suites/builds move to "
           "Hugging Face CPU'). Use hfjob -- <cmd> (repo-agnostic: it takes this checkout's pushed HEAD and runs the command on a cpu-upgrade job). "
           "Deploys (scripts/deploy.sh), vps-scratch-sweep and hfjob itself are allowed over ssh.")

# `ssh` as the command word of a segment (optional leading VAR=x), so a commit message or echo that merely names ssh passes.
SSH_CMD = re.compile(r"""(?:^|[;&|(\n])\s*(?:\w+=\S*\s+)*ssh\s""")
SEGMENT_SPLIT = re.compile(r"[;&|()\n`]+")
# Words that may sit before the real command inside the payload.
PREFIX = r"""(?:(?:\w+=\S*|sudo|nice(?:\s+-n\s*\d+)?|ionice(?:\s+-c\s*\d)?|time|env|timeout\s+\d+\w?|capture(?:\s+--prio\s+\d+)?\s+\S+|bash\s+-lc|sh\s+-c)\s+)*"""
HEAVY = re.compile(
    r"^" + PREFIX + r"(?:"
    r"npx\s+playwright\b|vite\s+build\b|(?:\S*/)?[Bb]lender\b|meshopt\S*|gltfpack\b"
    r"|npm\s+(?:run\s+)?test(?::[\w-]+)?\b|npm\s+run\s+(?:quality(?::[\w-]+)?|build|release-checks?)\b"
    r"|(?:node|tsx)\s+--test\b"
    r"|node\s+(?:\S*/)?scripts/(?:release-checks|[\w-]*(?:check|preview|gate|quality|battery|stills|record|build-|contact-sheet|herolook-)[\w-]*)\.mjs"
    r")")


SSH_ARG_OPTS = {"-i", "-o", "-p", "-l", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-m", "-E", "-S", "-W", "-B", "-e", "-I", "-O", "-Q", "-w"}


def _after_host(segment: str) -> str:
    """The remote command of the first ssh segment: drop the options (and their values) and the host."""
    tokens, i = segment.split(), 0
    while i < len(tokens):
        t = tokens[i]
        if t in SSH_ARG_OPTS:
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            i += 1   # the host
            break
    return " ".join(tokens[i:])


def ssh_heavy_reason(command: str) -> str:
    text = command or ""
    m = SSH_CMD.search(text)
    if not m:
        return ""
    for n, seg in enumerate(SEGMENT_SPLIT.split(text[m.end():])):
        seg = _after_host(seg) if n == 0 else seg
        if HEAVY.match(seg.strip().lstrip("'\" \t").lstrip()):
            return MESSAGE
    return ""

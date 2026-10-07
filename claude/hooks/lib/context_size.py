"""How big is this session's context, and what should the agent be told about it?

Measured 2026-09-22: lanes started the day near 85k and ended pinned at the 966k ceiling; every tool call
re-reads the whole context, so the day cost ~4,800M tokens against ~5.4M of actual generated output. Held near
200k the same work costs 1,707M — a 64% saving with no model or quality change. WARN_TOKENS is 300k rather than
that 200k because Dom asked for headroom on 2026-09-22; the arithmetic above is unchanged.

Context is a per-task budget, not a diary (Dom, 2026-09-22 16:55): a large audit may legitimately sit at 600k;
a seven-line fix at 860k is the waste. So the notice never asks for a mid-task restart - it fires at the task
boundary, when a PR is merged or the work is handed over.

The size comes from the transcript's own usage records (cache_read + cache_creation + input on the last
assistant message), so it is what the API actually charged for, not an estimate. Read from the file tail only.
"""
from __future__ import annotations

import json
import os
import re

WARN_TOKENS = 500_000   # Dom, 2026-09-27: raised from 400k (300k on 09-25, 200k on 09-22)
# The coordinating lanes refill fastest and lose the most working state on a restart (the PR merge order,
# the lane briefs), so they get headroom (Dom, 2026-09-27: Lead 750k, Strategy 700k, Deploy 600k; before that Lead and Strategy
# 600k, Deploy 500k from 09-26, when Lead filled fastest and restarted unnoticed at 500k, losing time).
# "lead" is the Lead lane when it is named from its session title rather than its ~/Developer/frankendom-* folder.
LANE_WARN_TOKENS = {"lead-catalogue": 750_000, "lead": 750_000, "expansion": 750_000, "strategy": 700_000, "deploy": 600_000}
URGENT_TOKENS = 800_000  # above every lane threshold, so "put this first" never fires at the first notice
TAIL_BYTES = 512 * 1024
# Also "~/Developer/Frankendom - Stats": the Stats session was opened in that folder, so its hooks get that cwd and
# the lowercase-only pattern made it no lane at all (2026-09-24). Keys are lowercased: "Stats" -> "stats".
# A leading "The" is skipped: "Frankendom - The Pit" is lane pit (docs/state/pit.md), not "the" (2026-09-29).
LANE_RE = re.compile(r"frankendom(?:-| - )(?:the )?([\w.-]+)", re.IGNORECASE)


def context_tokens(transcript_path: str) -> int:
    """Tokens the last API call in this session was charged for; 0 when unknown."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            usage = (json.loads(line).get("message") or {}).get("usage") or {}
        except ValueError:
            continue
        total = (usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                 + usage.get("input_tokens", 0))
        if total:
            return int(total)
    return 0


def lane_name(cwd: str) -> str:
    m = LANE_RE.search(str(cwd or ""))
    return m.group(1).lower() if m else "session"


TITLE_RE = re.compile(r'"customTitle":\s*"([^"]*)"')


def session_title(transcript_path: str) -> str:
    """The session's desktop title ("Frankendom - Lead Developer"); '' when none was recorded.
    Claude Code re-writes it through the transcript, so the tail usually has it; the head is the fallback."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="ignore")
            fh.seek(0)
            head = fh.read(TAIL_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return ""
    found = TITLE_RE.findall(tail) or TITLE_RE.findall(head)
    return found[-1] if found else ""


def session_lane(transcript_path: str, cwd: str) -> str:
    """The lane from the folder, or else from the title. Since 2026-09-25 the lanes run in desktop worktrees
    (.../frankendom/.claude/worktrees/funny-rhodes-...) whose folder names no lane, so only the title does."""
    lane = lane_name(cwd)
    if lane != "session":
        return lane
    return title_lane(session_title(transcript_path))


def title_lane(title: str) -> str:
    """The lane key a desktop title names. "Frankendom - Lead Dev (Expansion)" is its own lane (memory under
    ~/Developer/frankendom-expansion, 2026-10-07), not Lead, although both start with "Lead"."""
    if "(expansion)" in title.lower():
        return "expansion"
    lane = lane_name(title)
    return TITLE_LANES.get(lane, lane)


# A title's first word is not always the lane's key: "Multi Chars" keeps docs/state/multichar.md.
TITLE_LANES = {"multi": "multichar", "visuals": "world", "auditer": "code-quality", "auditor": "code-quality", "veteran": "veteran-polish",
               "hero": "herolook"}


def warn_tokens(lane: str) -> int:
    return LANE_WARN_TOKENS.get(lane, WARN_TOKENS)


def context_notice(transcript_path: str, cwd: str) -> tuple[str, bool]:
    """(line to inject, put_first). ('' , False) below the warn threshold."""
    tokens = context_tokens(transcript_path)
    lane = session_lane(transcript_path, cwd)
    warn = warn_tokens(lane)
    if tokens < warn:
        return "", False
    line = (f"# Context {tokens // 1000}k. When this task closes (PR merged or handed over), write "
            f"docs/state/{lane}.md + memory, then clear this session yourself and resume from them. Do not restart "
            f"mid-task. "
            f"Every tool call re-reads the whole context, so this session costs {tokens / warn:.1f}x a "
            f"{warn // 1000}k one "
            f"per call — context is a per-task budget, not a diary: a long audit may legitimately sit high, a "
            f"seven-line fix at {tokens // 1000}k is waste.")
    return line, tokens >= URGENT_TOKENS

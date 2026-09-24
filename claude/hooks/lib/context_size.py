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

WARN_TOKENS = 300_000   # Dom, 2026-09-22, in his own session: raised from 200k "to give some balance"
# The coordinating lanes refill fastest and lose the most working state on a restart (a deploy queue, the
# PR merge order), so they get headroom (Dom, 2026-09-24). Every other lane stays at WARN_TOKENS.
LANE_WARN_TOKENS = {"lead-catalogue": 350_000, "strategy": 350_000, "deploy": 350_000}
URGENT_TOKENS = 500_000
TAIL_BYTES = 512 * 1024
# Also "~/Developer/Frankendom - Stats": the Stats session was opened in that folder, so its hooks get that cwd and
# the lowercase-only pattern made it no lane at all (2026-09-24). Keys are lowercased: "Stats" -> "stats".
LANE_RE = re.compile(r"frankendom(?:-| - )([\w.-]+)", re.IGNORECASE)


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


def warn_tokens(lane: str) -> int:
    return LANE_WARN_TOKENS.get(lane, WARN_TOKENS)


def context_notice(transcript_path: str, cwd: str) -> tuple[str, bool]:
    """(line to inject, put_first). ('' , False) below the warn threshold."""
    tokens = context_tokens(transcript_path)
    lane = lane_name(cwd)
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

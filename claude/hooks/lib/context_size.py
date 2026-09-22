"""How big is this session's context, and what should the agent be told about it?

Measured 2026-09-22: lanes started the day near 85k and ended pinned at the 966k ceiling; every tool call
re-reads the whole context, so the day cost ~4,800M tokens against ~5.4M of actual generated output. Capping
sessions at 200k would have cost 1,707M — a 64% saving with no model or quality change.

The size comes from the transcript's own usage records (cache_read + cache_creation + input on the last
assistant message), so it is what the API actually charged for, not an estimate. Read from the file tail only.
"""
from __future__ import annotations

import json
import os
import re

WARN_TOKENS = 200_000
URGENT_TOKENS = 300_000
TAIL_BYTES = 512 * 1024
LANE_RE = re.compile(r"frankendom-([\w.-]+)")


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
    return m.group(1) if m else "session"


def context_notice(transcript_path: str, cwd: str) -> tuple[str, bool]:
    """(line to inject, put_first). ('' , False) below the warn threshold."""
    tokens = context_tokens(transcript_path)
    if tokens < WARN_TOKENS:
        return "", False
    lane = lane_name(cwd)
    line = (f"# Context {tokens // 1000}k — restart due. Finish the task in hand, write docs/state/{lane}.md and "
            f"your memory files so the next session can pick it up cold, then /clear. Every tool call re-reads "
            f"this whole context: a {tokens // 1000}k session costs {tokens / WARN_TOKENS:.1f}x a 200k one per call.")
    return line, tokens >= URGENT_TOKENS

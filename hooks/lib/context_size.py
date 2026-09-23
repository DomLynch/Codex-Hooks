"""How big is this session's context, and what should the agent be told about it?

Ported from the Claude install on 2026-09-23. The arithmetic that motivates it is the same on both sides:
every tool call re-reads the whole context, so a session that grows unchecked pays for that context again on
every single call. A live Codex thread inspected on 2026-09-23 had billed 646,563,734 input tokens across the
session against 2,800,181 generated — the cost is almost entirely re-read, not output.

Context is a per-task budget, not a diary (Dom, 2026-09-22 16:55): a large audit may legitimately sit at 600k;
a seven-line fix at 860k is the waste. So the notice never asks for a mid-task restart — it fires at the task
boundary, when a PR is merged or the work is handed over.

The ONE thing that differs from the Claude module is where the number comes from. Claude writes
`{"message": {"usage": {"cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens"}}}` and the
three are summed. Codex writes a `token_usage_record` line whose `payload.usage.input_tokens` ALREADY includes
the cached portion (`cached_input_tokens` is a subset of it, not an addend), so summing them here would roughly
double the figure. Read `input_tokens` alone. Either way the number is what the API actually charged for the
last call, not an estimate, and it is read from the file tail only.
"""
from __future__ import annotations

import json
import os
import re

WARN_TOKENS = 300_000   # Dom, 2026-09-22, in his own session: raised from 200k "to give some balance"
URGENT_TOKENS = 500_000
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
            record = json.loads(line)
        except ValueError:
            continue
        usage = ((record.get("payload") or {}).get("usage")
                 or (record.get("message") or {}).get("usage") or {})
        if not isinstance(usage, dict):
            continue
        # Codex: input_tokens already includes cached_input_tokens. Claude: the three are disjoint.
        total = usage.get("input_tokens", 0)
        if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
            total += usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        if total:
            return int(total)
    return 0


def lane_name(cwd: str) -> str:
    m = LANE_RE.search(str(cwd or ""))
    return m.group(1) if m else "session"


def context_notice(transcript_path: str, cwd: str) -> tuple[str, bool]:
    """(line to inject, put_first). ('', False) below the warn threshold."""
    tokens = context_tokens(transcript_path)
    if tokens < WARN_TOKENS:
        return "", False
    lane = lane_name(cwd)
    line = (f"# Context {tokens // 1000}k. When this task closes (PR merged or handed over), write "
            f"docs/state/{lane}.md + your notes, then start a fresh session and resume from them. Do not "
            f"restart mid-task. Every tool call re-reads the whole context, so this session costs "
            f"{tokens / WARN_TOKENS:.1f}x a {WARN_TOKENS // 1000}k one per call — context is a per-task "
            f"budget, not a diary: a long audit may legitimately sit high, a seven-line fix at "
            f"{tokens // 1000}k is waste.")
    return line, tokens >= URGENT_TOKENS

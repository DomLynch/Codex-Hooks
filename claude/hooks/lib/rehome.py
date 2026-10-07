"""Re-home turns: let the Stop chain finish in under a second so change_directory can apply.

The desktop app applies mcp__ccd_directory__change_directory only if the session goes idle within ~6 s of the turn
ending (main.log 2026-09-22 10:34: "pending cwd apply: rejected (busy)" every second, then "failed: The session
stayed busy after the turn ended"). The Stop chain (mimo_audit → stop_orchestrator) takes longer than that even on a
chat turn, so the re-home never lands. A turn is a re-home turn when its last tool_use is change_directory, or when the
lane wrote a sentinel <scratchpad>/.rehome-pending before calling it (deleted here on the way out). Nothing else about
either hook changes: the next turn is reviewed and gated as usual.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

REHOME_TOOL = "mcp__ccd_directory__change_directory"
SENTINEL = ".rehome-pending"
TAIL_BYTES = 256 * 1024


def _last_tool_use_name(transcript_path: str) -> str:
    """Name of the last tool_use in the transcript tail, or ''. Reads only the tail; no JSON parse of the whole file."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    from .claude_adapter import is_turn_start   # lazy: claude_adapter is heavier than this fast-exit path needs

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        # The exemption belongs to the CURRENT user turn only: a user prompt after the last tool_use means this
        # turn made no tool call of its own (an earlier turn's change_directory must not skip this turn's review).
        if is_turn_start(entry):
            return ""
        if '"tool_use"' not in line:
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        names = [b.get("name", "") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if names:
            return names[-1]
    return ""


def _sentinels(session_id: str) -> list[Path]:
    found = []
    env = os.environ.get("CLAUDE_SCRATCHPAD")
    if env:
        found.append(Path(env) / SENTINEL)
    if session_id:
        found += [Path(p) for p in glob.glob(f"/private/tmp/claude-*/*/{session_id}/scratchpad/{SENTINEL}")]
    return [p for p in found if p.is_file()]


def is_rehome_turn(payload: dict) -> str:
    """Why this is a re-home turn ('' if it is not). Cheap: a tail read and a glob."""
    session_id = str(payload.get("session_id") or "")
    sentinels = _sentinels(session_id)
    if sentinels:
        for p in sentinels:
            try:
                p.unlink()
            except OSError:
                pass
        return f"sentinel {sentinels[0]}"
    if _last_tool_use_name(str(payload.get("transcript_path") or "")) == REHOME_TOOL:
        return f"last tool_use is {REHOME_TOOL}"
    return ""

"""
Claude Code → Codex payload normalizer.

Claude Code hook payloads differ from Codex's in three key ways:
  1. No `turn_id` — we synthesize one from session_id + transcript hash
  2. No `last_assistant_message` — we extract from transcript JSONL
  3. Transcript JSONL schema differs (`type=user|assistant` vs Codex's `response_item`)

This module wraps Claude payloads so downstream audit logic can pretend it's Codex.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .transcript import compact_task_entries


ELISION_MARKER = "\n...[slice-elided by compactor - full content was present in the actual tool call]...\n"


def compact(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + ELISION_MARKER + text[-limit // 2 :]


def synthesize_turn_id(session_id: str, transcript_path: str, stop_hook_active: bool = False) -> str:
    """Stable turn id for one Claude turn. Anchored on:
       - session_id (stable across turns of same session)
       - transcript file size + mtime (changes between turns, stable within a turn)
       - stop_hook_active (differentiates retry attempts within the same turn)
    """
    parts = [session_id or "unknown"]
    try:
        st = Path(transcript_path).stat() if transcript_path else None
        if st:
            parts.append(str(int(st.st_size)))
            parts.append(str(int(st.st_mtime)))
    except Exception:
        parts.append(str(int(time.time() / 60)))  # 1-minute bucket fallback
    parts.append("retry" if stop_hook_active else "first")
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


def read_claude_transcript_lines(path: str) -> list[dict]:
    """Parse Claude Code transcript JSONL. Returns the raw entries."""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    entries: list[dict] = []
    try:
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return entries


def extract_last_assistant_message(transcript_path: str) -> str:
    """Pull the most recent assistant message text from a Claude transcript."""
    for entry in reversed(read_claude_transcript_lines(transcript_path)):
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text") or ""))
            text = "\n".join(p for p in parts if p)
            if text:
                return text
    return ""


def latest_task_slice_claude(transcript_path: str, last_assistant_message: str = "", max_chars: int = 4000) -> str:
    """Build the 'current task' slice from a Claude transcript.

    Strategy: walk back to the last `user` entry and include everything from there.
    Falls back to last_assistant_message if transcript is unreadable.
    """
    entries = read_claude_transcript_lines(transcript_path)
    if not entries:
        return last_assistant_message or ""

    # Tool results also use type=user; only a real prompt starts a new task.
    last_user_idx = -1
    for idx in range(len(entries) - 1, -1, -1):
        entry = entries[idx]
        content = (entry.get("message") or {}).get("content")
        tool_result_only = isinstance(content, list) and content and all(
            isinstance(item, dict) and item.get("type") == "tool_result" for item in content
        )
        if entry.get("type") == "user" and not tool_result_only:
            last_user_idx = idx
            break
    slice_entries = entries[last_user_idx:] if last_user_idx >= 0 else entries[-30:]

    out: list[tuple[str, str]] = []
    for entry in slice_entries:
        etype = entry.get("type", "")
        msg = entry.get("message") or {}
        content = msg.get("content")
        if etype == "user":
            if isinstance(content, str):
                out.append(("USER", content))
            elif isinstance(content, list):
                texts = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("content") or item.get("text") or "")[:1500]
                    if item.get("type") == "tool_result":
                        out.append(("TOOL OUTPUT", text))
                    elif text:
                        texts.append(text)
                if texts:
                    out.append(("USER", "\n".join(texts)))
        elif etype == "assistant":
            if isinstance(content, str):
                out.append(("ASSISTANT", content))
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype == "text":
                        out.append(("ASSISTANT", str(item.get("text") or "")))
                    elif itype == "tool_use":
                        name = item.get("name", "?")
                        inp = compact(json.dumps(item.get("input", {})), 800)
                        out.append(("TOOL CALL", f"{name}: {inp}"))
        elif etype == "tool_result" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    raw = str(item.get("content") or "")[:1000]
                    out.append(("TOOL OUTPUT", raw))

    return compact_task_entries(out, max_chars) if out else last_assistant_message[-max_chars:]


def normalize_payload(raw: dict) -> dict:
    """Return a Codex-shaped payload dict, with Claude-derived fields filled in."""
    session_id = raw.get("session_id") or "unknown"
    transcript_path = raw.get("transcript_path") or ""
    stop_hook_active = bool(raw.get("stop_hook_active", False))
    cwd = raw.get("cwd") or ""

    # Pass through if already in Codex shape (defensive — works even if Codex calls these)
    turn_id = raw.get("turn_id") or synthesize_turn_id(session_id, transcript_path, stop_hook_active)
    last_msg = raw.get("last_assistant_message") or extract_last_assistant_message(transcript_path)

    out: dict[str, Any] = dict(raw)
    out["turn_id"] = turn_id
    out["last_assistant_message"] = last_msg
    out["session_id"] = session_id
    out["cwd"] = cwd
    out["stop_hook_active"] = stop_hook_active
    return out


def changed_files_in_claude_post_tool(payload: dict) -> list[Path]:
    """Extract file paths touched by Edit/Write/MultiEdit. Used by post_tool quality check."""
    tool = payload.get("tool_name") or ""
    inp = payload.get("tool_input") or {}
    if tool == "Edit":
        path = inp.get("file_path")
        return [Path(path)] if path else []
    if tool == "Write":
        path = inp.get("file_path")
        return [Path(path)] if path else []
    if tool == "MultiEdit":
        path = inp.get("file_path")
        return [Path(path)] if path else []
    if tool == "NotebookEdit":
        path = inp.get("notebook_path")
        return [Path(path)] if path else []
    return []

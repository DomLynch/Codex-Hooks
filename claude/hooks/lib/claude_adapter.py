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
import re
import time
from pathlib import Path
from typing import Any

from .transcript import _content_text, compact_latest_task, strip_hook_prompts, tool_entry


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


def is_turn_start(entry: dict) -> bool:
    """A real user prompt (not a tool_result carrier, not bare hook feedback) starts a turn."""
    content = (entry.get("message") or {}).get("content")
    tool_result_only = isinstance(content, list) and content and all(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content
    )
    text = _content_text(content)
    hook_feedback = text.lstrip().startswith("<hook_prompt") and not strip_hook_prompts(text)
    return entry.get("type") == "user" and not tool_result_only and not hook_feedback


def entries_from_lines(slice_entries: list[dict]) -> list[tuple[str, str]]:
    """(role, body) entries for an arbitrary run of transcript lines — the single place
    Claude's JSONL shape is interpreted. latest_task_entries_claude() and the corpus
    replay harness both go through here so they can never disagree."""
    out: list[tuple[str, str]] = []
    calls: dict[str, str] = {}
    for entry in slice_entries:
        etype = entry.get("type", "")
        msg = entry.get("message") or {}
        content = msg.get("content")
        if etype == "user":
            if _content_text(content).lstrip().startswith("<hook_prompt"):
                remaining = strip_hook_prompts(_content_text(content))
                if remaining:
                    out.append(("USER", remaining))
                    continue
                out.append(("PRIOR REVIEW (not a new request)", re.sub(r"</?hook_prompt\b[^>]*>", "", _content_text(content))))
                continue
            if isinstance(content, str):
                out.append(("USER", content))
            elif isinstance(content, list):
                result_only = content and all(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)
                if not result_only:
                    out.append(("USER", _content_text(content) or "[non-text user message]"))
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_result":
                        result = tool_entry({"type": "function_call_output", "call_id": item.get("tool_use_id"), "output": item.get("content")}, calls)
                        if result:
                            out.append(result)
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
                        result = tool_entry({"type": "function_call", "call_id": item.get("id"), "name": item.get("name"), "arguments": json.dumps(item.get("input", {}))}, calls)
                        if result:
                            out.append(result)
        elif etype == "tool_result" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    result = tool_entry({"type": "function_call_output", "call_id": item.get("tool_use_id"), "output": item.get("content")}, calls)
                    if result:
                        out.append(result)

    return out


_NOT_OWNER_PREFIXES = ("Another Claude session sent", "<", "Stop hook feedback", "[Claude ", "[GPT-", "[gpt-",
                       "[Repository Quality", "[SYSTEM NOTIFICATION", "[Cross-session", "[MiniMax")
_AUTHORIZATION_RE = re.compile(
    r"\b(go live|get (it|us|them|this) (all )?(merged|deployed|live)|push (it )?live|(deploy|merge|ship|release) (it|this|them|now)|"
    r"one deployer|you are the deployer|go ahead|approved|(ok |yes )?do it|yes,? (go|do|proceed|approved)|full (approval|authority)|delegate|"
    r"do whatever (u|you) need|take the decision|you decide|use your judgement|i trust you)\b", re.IGNORECASE)
_PEER_START_RE = re.compile(r"^Another Claude session sent|<cross-session-message\b")
_COMMAND_ARG_RE = re.compile(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"')
# Hard-to-reverse shared-state actions: merge into the base branch, deploy, rewrite a pushed
# branch. `git revert` / `reset --hard` are deliberately absent: on a lane branch they are
# ordinary edits and the gate cannot see which branch is checked out. Matched at the start of a
# shell segment so a mention (`grep -v deploy.sh`, `echo "gh pr merge"`) is not an execution.
_CONSEQUENTIAL_RE = re.compile(
    r"^(?:sudo\s+|env\s+\S+=\S+\s+|bash\s+|sh\s+|\./)?(?:"
    r"gh pr merge\b|scripts/deploy\.sh\b|git push\b(?=.*\s(?:--force|-f|--force-with-lease)\b))")


def owner_instructions(transcript_path: str, before: str = "", limit: int = 5, max_chars: int = 240) -> list[tuple[str, str]]:
    """The user's own messages in this session that read like an authorization (deploy, merge, ship,
    go live ...), newest first, each (timestamp, text). Peer-session messages, hook feedback, monitor
    events and tool results are excluded by prefix, so a peer can never appear here as the owner.
    `before` (ISO) limits to messages sent before the current turn started."""
    try:
        lines = read_claude_transcript_lines(transcript_path)
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for entry in reversed(lines):
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text or text.startswith(_NOT_OWNER_PREFIXES):
            continue
        ts = str(entry.get("timestamp") or "")
        if before and ts >= before:
            continue
        # An instruction is short and in the owner's own words. Long messages are pastes (reports, peer text,
        # the agent's own words quoted back) whose buried "do it" / "ship it" is not the owner speaking; messages
        # that are mostly questions, and scheduled/monitor prompts quoting the owner, are not instructions either.
        if len(text) > 400 or text.count("?") >= 2 or text.startswith(("One-hour check-in", "Overnight ", "Morning re-check")):
            continue
        if _AUTHORIZATION_RE.search(text[:240]):
            out.append((ts[11:16] if len(ts) >= 16 else ts, " ".join(text.split())[:max_chars]))
            if len(out) >= limit:
                break
    return out


def executed_consequential_commands(entries: list[tuple[str, str]]) -> list[str]:
    """Merge / deploy / revert / force-push commands this turn actually ran (Bash tool calls)."""
    found: list[str] = []
    for role, body in entries:
        if role != "TOOL CALL" or not body.startswith("Bash"):
            continue
        m = _COMMAND_ARG_RE.search(body)
        if not m:
            continue
        try:
            command = json.loads(f'"{m.group(1)}"')
        except ValueError:
            command = m.group(1)
        for segment in re.split(r"&&|\|\||;|\n|\|", command):
            seg = segment.strip()
            hit = _CONSEQUENTIAL_RE.match(seg)
            if hit and hit.group(0) not in found:
                found.append(hit.group(0).strip())
    return found


def peer_action_without_in_session_approval(entries: list[tuple[str, str]], transcript_path: str, turn_started: str = "") -> str:
    """Diagnostic for a peer-started consequential action without in-session owner approval.
    Approval from an earlier session is not visible here, so this must not block Stop."""
    users = [body for role, body in entries if role == "USER"]
    if not users or not _PEER_START_RE.search(users[0][:300]):
        return ""
    commands = executed_consequential_commands(entries)
    if not commands:
        return ""
    if owner_instructions(transcript_path, before=turn_started, limit=1):
        return ""
    return (f"peer-started command={', '.join(commands)}; no earlier in-session owner approval found. "
            "Standing approval in another session remains possible.")


def latest_turn_started_at(transcript_path: str) -> str:
    """ISO timestamp of the latest turn's first entry (the user message that started it), or ""."""
    try:
        lines = read_claude_transcript_lines(transcript_path)
    except Exception:
        return ""
    for entry in reversed(lines):
        if is_turn_start(entry):
            return str(entry.get("timestamp") or "")
    return ""


def latest_task_entries_claude(transcript_path: str, include_previous: bool = False) -> list[tuple[str, str]]:
    """Current-turn (role, body) entries from a Claude transcript, before compaction."""
    """Build the 'current task' slice from a Claude transcript.

    Strategy: walk back to the last `user` entry and include everything from there.
    Falls back to last_assistant_message if transcript is unreadable.
    """
    entries = read_claude_transcript_lines(transcript_path)
    if not entries:
        return []

    # Tool results also use type=user; only a real prompt starts a new task.
    last_user_idx = -1
    for idx in range(len(entries) - 1, -1, -1):
        entry = entries[idx]
        content = (entry.get("message") or {}).get("content")
        tool_result_only = isinstance(content, list) and content and all(
            isinstance(item, dict) and item.get("type") == "tool_result" for item in content
        )
        hook_feedback = _content_text(content).lstrip().startswith("<hook_prompt") and not strip_hook_prompts(_content_text(content))
        if entry.get("type") == "user" and not tool_result_only and not hook_feedback:
            last_user_idx = idx
            break
    slice_entries = entries[last_user_idx:] if last_user_idx >= 0 else entries[-30:]
    if include_previous:
        slice_entries = entries

    return entries_from_lines(slice_entries)


def latest_task_slice_claude(transcript_path: str, last_assistant_message: str = "", max_chars: int = 4000,
                             include_previous: bool = False) -> str:
    out = latest_task_entries_claude(transcript_path, include_previous)
    return compact_latest_task(out, max_chars, include_previous) if out else last_assistant_message[-max_chars:]


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

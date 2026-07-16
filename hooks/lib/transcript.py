from __future__ import annotations

import json
import re
from pathlib import Path


MAX_TRANSCRIPT_CHARS = 4000
HOOK_PROMPT_RE = re.compile(r"<hook_prompt\b[^>]*>.*?</hook_prompt>", re.DOTALL | re.IGNORECASE)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def sanitize(text: str) -> str:
    text = re.sub(r"tp-[A-Za-z0-9]+", "tp-<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    text = re.sub(r"ghp_[A-Za-z0-9_]+", "ghp_<redacted>", text)
    text = re.sub(r'"encrypted_content":\s*"[^"]+"', '"encrypted_content": "<omitted>"', text)
    text = HOOK_PROMPT_RE.sub("<hook_prompt omitted>", text)
    return text.replace("<hook_prompt", "<hook-prompt")


def strip_hook_prompts(text: str) -> str:
    return HOOK_PROMPT_RE.sub("", text or "").strip()


def compact(text: str, limit: int) -> str:
    text = sanitize(text)
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[slice-elided by compactor - full content was present in the actual tool call]...\n" + text[-limit // 2 :]


def _render_entry(role: str, body: str) -> str:
    return f"{role}: {body}" if role == "TOOL CALL" else f"{role}:\n{body}"


def _bounded_compact(text: str, limit: int) -> str:
    text = sanitize(text)
    if len(text) <= limit:
        return text
    marker = "\n...[slice-elided by compactor]...\n"
    body_budget = max(0, limit - len(marker))
    head = body_budget // 2
    tail = body_budget - head
    return (text[:head] + marker + (text[-tail:] if tail else ""))[:limit]


def compact_task_entries(entries: list[tuple[str, str]], max_chars: int) -> str:
    text = sanitize("\n\n".join(_render_entry(role, body) for role, body in entries))
    if len(text) <= max_chars:
        return text
    first_role, first_body = entries[0]
    head_budget = max_chars // 4
    first = sanitize(_render_entry(first_role, first_body))
    if len(first) > head_budget:
        first = first[:head_budget] + "\n...[head truncated]..."
    receipt_budget = max_chars // 4
    receipts = sanitize("\n\n".join(
        _render_entry(role, body) for role, body in entries[1:] if role == "TOOL OUTPUT"
    ))[-receipt_budget:]
    receipt_block = f"\n\n...[verification receipts preserved]...\n\n{receipts}" if receipts else ""
    marker = "\n\n...[older task details omitted to stay focused]...\n\n"
    tail_budget = max(0, max_chars - len(first) - len(receipt_block) - len(marker))
    final = next((_render_entry(role, body) for role, body in reversed(entries) if role == "ASSISTANT"), "")
    recent = "\n\n".join(_render_entry(role, body) for role, body in entries[-3:] if role != "TOOL OUTPUT")
    tail = _bounded_compact("\n\n".join(part for part in (final, recent) if part), tail_budget) if tail_budget else ""
    return (first + receipt_block + marker + tail)[:max_chars]


def latest_task_slice(transcript_path: str, fallback: str = "", max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    if not transcript_path:
        return sanitize(fallback)[-max_chars:]
    path = Path(transcript_path)
    if not path.exists():
        return sanitize(fallback)[-max_chars:]
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return sanitize(fallback)[-max_chars:]

    entries: list[tuple[str, str]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") or {}
        item_type = payload.get("type")
        if item_type == "message":
            role = str(payload.get("role") or "message").upper()
            text = _content_text(payload.get("content"))
            if role == "USER" and text.lstrip().startswith("<hook_prompt"):
                text = strip_hook_prompts(text) or "[hook feedback omitted]"
            if text:
                entries.append((role, text))
        elif item_type == "function_call":
            args = compact(str(payload.get("arguments") or ""), 800)
            entries.append(("TOOL CALL", f"{payload.get('name')}: {args}"))
        elif item_type == "function_call_output":
            entries.append(("TOOL OUTPUT", compact(str(payload.get("output") or ""), 1000)))

    if not entries:
        return sanitize(fallback)[-max_chars:]
    start = 0
    for idx in range(len(entries) - 1, -1, -1):
        if entries[idx][0] == "USER":
            start = idx
            break
    task_entries = entries[start:]
    return compact_task_entries(task_entries, max_chars)

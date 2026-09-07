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


def tool_entry(payload: dict, calls: dict[str, str]) -> tuple[str, str] | None:
    kind = payload.get("type")
    call_id = str(payload.get("call_id") or "unknown")
    if kind in {"function_call", "custom_tool_call"}:
        name = str(payload.get("name") or "tool")
        args = str(payload.get("arguments") or payload.get("input") or "")
        calls[call_id] = compact(f"{name}: {args}", 300)
        cap = 8000 if name.lower() in {"apply_patch", "edit", "write", "multi_edit", "multiedit", "apply_diff"} else 800
        return "TOOL CALL", f"{name}: [{call_id}] {compact(args, cap)}"
    if kind in {"function_call_output", "custom_tool_call_output"}:
        output = payload.get("output") or ""
        text = _content_text(output) if isinstance(output, list) else str(output)
        return "TOOL OUTPUT", f"[{call_id}] {calls.get(call_id, 'command outside slice')}\n{compact(text, 1000)}"
    return None


def _bounded_compact(text: str, limit: int, marker: str = "\n...[slice-elided by compactor]...\n") -> str:
    text = sanitize(text)
    if len(text) <= limit:
        return text
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
        first = _bounded_compact(first, head_budget, "\n...[head truncated]...\n")
    receipt_budget = max_chars // 4
    outputs = [body for role, body in entries if role == "TOOL OUTPUT"][-3:]
    receipts = "\n".join(_bounded_compact(body, receipt_budget // len(outputs) - 1) for body in outputs) if outputs else ""
    receipt_block = f"\n\n...[verification receipts preserved]...\n\n{receipts}" if receipts else ""
    marker = "\n\n...[older task details omitted to stay focused]...\n\n"
    tail_budget = max(0, max_chars - len(first) - len(receipt_block) - len(marker))
    final = next((_render_entry(role, body) for role, body in reversed(entries) if role == "ASSISTANT"), "")
    recent = "\n\n".join(_render_entry(role, body) for role, body in entries[-3:] if role not in {"TOOL OUTPUT", "ASSISTANT"})
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
    calls: dict[str, str] = {}
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
                remaining = strip_hook_prompts(text)
                if remaining:
                    entries.append(("USER", remaining))
                    continue
                role = "PRIOR REVIEW (not a new request)"
                text = re.sub(r"</?hook_prompt\b[^>]*>", "", text)
            if text:
                entries.append((role, text))
        else:
            entry = tool_entry(payload, calls)
            if entry:
                entries.append(entry)

    if not entries:
        return sanitize(fallback)[-max_chars:]
    start = 0
    for idx in range(len(entries) - 1, -1, -1):
        if entries[idx][0] == "USER":
            start = idx
            break
    task_entries = entries[start:]
    return compact_task_entries(task_entries, max_chars)

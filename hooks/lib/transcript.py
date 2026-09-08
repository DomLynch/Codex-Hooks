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


def _tool_output_text(output: object) -> str:
    text = _content_text(output) if isinstance(output, list) else str(output)
    parts = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            record = None
        if isinstance(record, dict) and "chunk_id" in record and isinstance(record.get("output"), str):
            status = {key: value for key, value in record.items()
                      if key not in {"chunk_id", "wall_time_seconds", "original_token_count", "output"}}
            line = json.dumps(status) + "\n" + record["output"]
        parts.append(line)
    return "\n".join(parts)


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
        text = _tool_output_text(output)
        command = _bounded_compact(calls.get(call_id, "command outside slice"), 120)
        return "TOOL OUTPUT", f"[{call_id}] {command}\n{compact(text, 1000)}"
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
    marker = "\n\n...[older task details omitted to stay focused]...\n\n"
    final = next((_render_entry(role, body) for role, body in reversed(entries) if role == "ASSISTANT"), "")
    recent = "\n\n".join(_render_entry(role, body) for role, body in entries[-3:] if role not in {"TOOL OUTPUT", "ASSISTANT"})
    tail = _bounded_compact("\n\n".join(part for part in (final, recent) if part), max_chars // 2)
    outputs = [body for role, body in entries if role == "TOOL OUTPUT"]
    # Retain the initial verification as well as the latest results, in order.
    selected = outputs if len(outputs) <= 4 else outputs[:1] + outputs[-3:]
    header = (f"\n\n...[verification receipts preserved: {len(selected)}/{len(outputs)}; "
              f"omitted tool outputs={len(outputs) - len(selected)}; excerpts only]...\n\n") if outputs else ""
    receipt_budget = max(0, max_chars - len(first) - len(tail) - len(marker) - len(header))
    excerpts = [""] * len(selected)
    for remaining, idx in zip(range(len(selected), 0, -1), sorted(range(len(selected)), key=lambda i: len(selected[i]))):
        budget = max(0, receipt_budget // remaining - 1)
        body = selected[idx]
        # Keep explicit status lines visible even when head/tail elision cuts the result.
        status = "\n".join(line for line in body.splitlines()
                           if re.search(r"exit_code|Process exited|\bFAILED\b|\bERROR\b", line))
        status = _bounded_compact(status, budget // 2)
        prefix = "TOOL OUTPUT:\n" + ("[status excerpts]\n" + status + "\n" if status else "")
        excerpts[idx] = prefix + _bounded_compact(body, max(0, budget - len(prefix)))
        receipt_budget -= len(excerpts[idx]) + 1
    receipts = "\n".join(excerpts)
    receipt_block = header + receipts
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

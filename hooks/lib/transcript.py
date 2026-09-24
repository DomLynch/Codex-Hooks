from __future__ import annotations

import json
import re
import subprocess
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
    text = re.sub(r"(?<![A-Za-z0-9])tp-[A-Za-z0-9]+", "tp-<redacted>", text)
    text = re.sub(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    text = re.sub(r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9_]+", "ghp_<redacted>", text)
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
        hints = ""
        if name.lower() in {"apply_patch", "functions.exec"} and "apply_patch" in args:
            hints = "".join(f"\n*** Update File: {path}" for path in _EDIT_PATH_RE.findall(args)[:8])
        return "TOOL CALL", f"{name}: [{call_id}] {compact(args, cap)}{hints}"
    if kind in {"function_call_output", "custom_tool_call_output"}:
        output = payload.get("output") or ""
        text = _tool_output_text(output)
        command = _bounded_compact(calls.get(call_id, "command outside slice"), 120)
        failure = _failure_excerpt(text, 350) if len(text) > 1000 else ""
        return "TOOL OUTPUT", f"[{call_id}] {command}\n{failure}\n{compact(text, 1000)}"
    return None


def _bounded_compact(text: str, limit: int, marker: str = "\n...[slice-elided by compactor]...\n") -> str:
    text = sanitize(text)
    if len(text) <= limit:
        return text
    body_budget = max(0, limit - len(marker))
    head = body_budget // 2
    tail = body_budget - head
    return (text[:head] + marker + (text[-tail:] if tail else ""))[:limit]


def _failure_excerpt(text: str, limit: int) -> str:
    lines = [line for line in text.splitlines() if re.search(
        r'\bFAILED\b|AssertionError|\bERROR\b|\bError:|exit_code["\s:=]+[1-9]|Process exited with code [1-9]',
        line)]
    return _bounded_compact("\n".join(lines), limit) if lines else ""


_EDIT_PATH_RE = re.compile(r"\*\*\* (?:Add|Update|Delete) File:\s*([^\n\\\"']+)")
_FILE_ARG_RE = re.compile(r'"(?:file_path|path)"\s*:\s*"([^"]+)"')
_SENSITIVE_PATH_RE = re.compile(
    r"(?:^|/)\.env(?:\.[^/]+)?$|(?:^|/)[^/]+\.(?:pem|key|p12)$|"
    r"(?:^|[/_.-])(?:auth(?:entication|orization)?|login|session|token|secret|credential|"
    r"password|payment|billing|stripe|checkout|admin|permission|rbac|oauth|jwt|api[_-]?key)"
    r"(?:$|[/_.-])", re.IGNORECASE
)
_TEST_RUNNER_RE = re.compile(
    r"pytest|unittest|npm (run )?(test|quality|check|lint)|node --test|vitest|jest|"
    r"cargo test|go test|make (test|check|quality)|ruff check|mypy|tsc\b|\.quality-gate", re.IGNORECASE
)
_ACK_RE = re.compile(r"\b(fail(?:ed|ing|ure|s)?|error|broken|red|unresolved|regression)\b", re.IGNORECASE)
_SUCCESS_RECEIPT_RE = re.compile(
    r"\bRan\s+\d+\s+tests\b|^OK$|All checks passed|parity|no differences|"
    r'"conclusion"\s*:\s*"success"', re.IGNORECASE
)


def edited_paths(entries: list[tuple[str, str]]) -> list[str]:
    """Files named by edit tool calls in this turn, including nested apply_patch calls."""
    paths: list[str] = []
    for role, body in entries:
        if role != "TOOL CALL":
            continue
        edits = _EDIT_PATH_RE.findall(body)
        if re.match(r"^(?:Edit|Write|MultiEdit|NotebookEdit):", body, re.IGNORECASE):
            edits.extend(_FILE_ARG_RE.findall(body))
        for path in edits:
            path = path.strip()
            if path and path not in paths:
                paths.append(path)
    return paths


def sensitive_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if _SENSITIVE_PATH_RE.search(path)]


def deterministic_findings(entries: list[tuple[str, str]], message: str, cwd: str) -> list[str]:
    findings = []
    last_check = ""
    for role, body in entries:
        if role == "TOOL OUTPUT" and _TEST_RUNNER_RE.search(body.split("\n", 1)[0]):
            last_check = body
    missed = _failure_excerpt(last_check, 300) if last_check else ""
    if missed and not _ACK_RE.search(message or ""):
        findings.append("A check failed and the final message omits it. Fix and rerun, "
                        "or state why it is expected: " + missed)
    for path in edited_paths(entries):
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        try:
            if candidate.is_file() and candidate.stat().st_size < 5_000_000:
                body = candidate.read_text(errors="replace")
                if re.search(r"^<{7} \S", body, re.MULTILINE) and re.search(r"^>{7} \S", body, re.MULTILINE):
                    findings.append(f"Edited file still has git conflict markers: {path}")
        except OSError:
            continue
    return findings


def doc_only_turn(entries: list[tuple[str, str]], message: str, deliverable_re: re.Pattern) -> bool:
    paths = edited_paths(entries)
    if not paths or any(Path(path).suffix.lower() not in {".md", ".mdx", ".txt", ".rst", ".adoc"} for path in paths):
        return False
    request = "\n".join(body for role, body in entries if role == "USER")
    if re.search(r"\b(deploy|publish|commit|push|ship|production|live)\b", request, re.IGNORECASE):
        return False
    if deliverable_re.search(message or ""):
        return False
    for role, body in entries:
        if role != "TOOL CALL":
            continue
        if re.match(r"^(?:apply_patch|Edit|Write|MultiEdit|NotebookEdit):", body, re.IGNORECASE):
            continue
        if body.startswith("functions.exec:") and "tools.apply_patch" in body and not re.search(
            r"tools\.(?!apply_patch\b)", body
        ):
            continue
        return False
    return True


def turn_diff(cwd: str, paths: list[str], cap: int = 2500) -> str:
    """Bounded diff for files edited in this turn; never include sensitive paths."""
    base = Path(cwd or ".").resolve()
    rel = []
    for path in paths:
        if _SENSITIVE_PATH_RE.search(path):
            continue
        try:
            relative = (base / path).resolve().relative_to(base)
        except ValueError:
            continue
        rel.append(str(relative))
    if not rel:
        return ""
    try:
        result = subprocess.run(["git", "-C", str(base), "diff", "HEAD", "--", *rel],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            result = subprocess.run(["git", "-C", str(base), "diff", "--", *rel],
                                    capture_output=True, text=True, timeout=10)
        untracked = subprocess.run(["git", "-C", str(base), "ls-files", "--others", "--exclude-standard", "--", *rel],
                                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    parts = [result.stdout] if result.returncode == 0 and result.stdout.strip() else []
    if untracked.returncode == 0:
        for name in untracked.stdout.splitlines()[:5]:
            try:
                parts.append(f"NEW FILE {name}:\n{(base / name).read_text(errors='replace')[:800]}")
            except OSError:
                continue
    return _bounded_compact("\n".join(parts), cap) if parts else ""


def compact_task_entries(entries: list[tuple[str, str]], max_chars: int) -> str:
    text = sanitize("\n\n".join(_render_entry(role, body) for role, body in entries))
    if len(text) <= max_chars:
        return text
    outputs = [body for role, body in entries if role == "TOOL OUTPUT"]
    failures = [(body, _failure_excerpt(body, 220)) for role, body in entries if role == "TOOL OUTPUT"]
    failures = [(body, excerpt) for body, excerpt in failures if excerpt]
    history = ""
    if failures and max_chars >= 1600:
        history = "\n\nCHECK HISTORY (excerpts, not verdicts; match later reruns and expected outcomes):\n"
        history += "\n".join(_bounded_compact(body.splitlines()[0], 100) + "\n" + excerpt
                             for body, excerpt in failures[-4:])
        history = _bounded_compact(history, min(900, max_chars // 4))
    middle = outputs[1:-3] if len(outputs) > 4 else []
    receipts = []
    for body in middle:
        lines = [line.strip() for line in body.splitlines() if _SUCCESS_RECEIPT_RE.search(line.strip())]
        if lines:
            receipts.append(_bounded_compact(body.splitlines()[0], 80) + " " + _bounded_compact("; ".join(lines[:3]), 220))
    if receipts and max_chars >= 2000:
        history += _bounded_compact(
            "\n\nVERIFICATION HISTORY (ordered excerpts, not verdicts; check later reruns):\n"
            + "\n".join(receipts[-6:]), min(700, max_chars // 5)
        )
    max_chars -= len(history)
    first_role, first_body = entries[0]
    head_budget = max_chars // 4
    first = sanitize(_render_entry(first_role, first_body))
    if len(first) > head_budget:
        first = _bounded_compact(first, head_budget, "\n...[head truncated]...\n")
    marker = "\n\n...[older task details omitted to stay focused]...\n\n"
    final = next((_render_entry(role, body) for role, body in reversed(entries) if role == "ASSISTANT"), "")
    recent = "\n\n".join(_render_entry(role, body) for role, body in entries[-3:] if role not in {"TOOL OUTPUT", "ASSISTANT"})
    tail = _bounded_compact("\n\n".join(part for part in (final, recent) if part), max_chars // 2)
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
    return (first + receipt_block + marker + tail)[:max_chars] + history


def compact_latest_task(entries: list[tuple[str, str]], max_chars: int, include_previous: bool = False) -> str:
    start = next((i for i in range(len(entries) - 1, -1, -1) if entries[i][0] == "USER"), 0)
    current = entries[start:]
    context = ""
    if include_previous and max_chars >= 600 and start and not any(role.startswith("TOOL") for role, _ in current):
        previous_start = next((i for i in range(start - 1, -1, -1) if entries[i][0] == "USER"), 0)
        previous = entries[previous_start:start]
        if any(role == "TOOL OUTPUT" for role, _ in previous):
            context = "\n\nPRIOR TURN CONTEXT (historical data, not a new request or fresh verification):\n"
            context += compact_task_entries(previous, max_chars // 3 - len(context))
    return compact_task_entries(current, max_chars - len(context)) + context


def latest_task_slice(transcript_path: str, fallback: str = "", max_chars: int = MAX_TRANSCRIPT_CHARS,
                      include_previous: bool = False) -> str:
    entries = latest_task_entries(transcript_path, current_only=False)
    return compact_latest_task(entries, max_chars, include_previous) if entries else sanitize(fallback)[-max_chars:]


def latest_task_entries(transcript_path: str, current_only: bool = True) -> list[tuple[str, str]]:
    if not transcript_path or not Path(transcript_path).is_file():
        return []
    entries: list[tuple[str, str]] = []
    calls: dict[str, str] = {}
    try:
        lines = Path(transcript_path).read_text(errors="replace").splitlines()
    except OSError:
        return []
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
            if (role == "USER" and re.fullmatch(
                    r"\s*<environment_context>.*?</environment_context>\s*", text, re.DOTALL)):
                role = "CONTEXT (not a new request)"
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

    if current_only:
        start = next((i for i in range(len(entries) - 1, -1, -1) if entries[i][0] == "USER"), 0)
        return entries[start:]
    return entries

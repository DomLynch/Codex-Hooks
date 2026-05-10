#!/usr/bin/env python3
# ruff: noqa: E402
"""
Claude Code Stop hook: 3rd-party audit with MiMo-v2.5-Pro.

When Claude Code thinks it's done, this script feeds the recent transcript to MiMo
(via Xiaomi's OpenAI-compatible endpoint) for review. If MiMo finds issues,
the hook BLOCKS the stop and injects the review back into Claude Code so it
keeps working. Capped at MAX_ITERATIONS per session to prevent infinite loops.

Stdlib only — no third-party Python deps.

Protocol (Claude Code Stop hook):
  stdin  -> {
              "session_id": str, "transcript_path": str|null, "cwd": str,
              "hook_event_name": str, "model": str, "turn_id": str,
              "stop_hook_active": bool, "last_assistant_message": str|null
            }
  stdout -> {"decision": "block", "reason": "..."}   to keep Claude Code running
  exit 0 with empty stdout                           to allow stop
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import record_lesson
from lib.json_state import load_json_locked, update_json_locked
from lib.claude_adapter import normalize_payload, latest_task_slice_claude


# --- Tunables --------------------------------------------------------------
MAX_ITERATIONS = 3
MODEL = "mimo-v2.5-pro"
BASE_URL = "https://token-plan-sgp.xiaomimimo.com/v1"
API_KEY_ENV = "MIMO_API_KEY"
MAX_TRANSCRIPT_CHARS = 4_000    # latest task slice fed to MiMo
MIN_TRANSCRIPT_CHARS = 400      # below this, skip (probably a chat reply)
AUDIT_TIMEOUT_S = 45
AUDIT_MAX_ATTEMPTS = 2
AUDIT_RETRY_DELAY_S = 2
RETRY_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
TOOL_ACTIVITY_MARKERS = ("TOOL CALL", "TOOL OUTPUT", "function_call", "tool_use", "tool_result")

LOG_FILE = Path.home() / ".claude" / "logs" / "mimo_audit.log"
STATE_FILE = Path.home() / ".claude" / "state" / "audit_iterations.json"
VERDICT_FILE = Path.home() / ".claude" / "state" / "mimo_last_verdict.json"
# MiMo key is shared from ~/.codex/secrets — single source of truth across both hook systems.
KEY_FILE = Path.home() / ".codex" / "secrets" / "mimo_api_key"

AUDIT_PROMPT = """Audit Claude's latest task. Do not explain your reasoning.
Your first line must be exactly one of:
VERDICT: PASS
VERDICT: ISSUES

Check only the current task for:
- Bugs, edge cases, off-by-ones, broken logic
- Missed requirements from the user's original ask
- Unnecessary clarification, permission, or "should I continue?" questions when
  Claude Code had a reasonable reversible default and should have kept working
- Security issues (injection, leaked credentials, unsafe shell/sql)
- Tests or validation that should exist but don't
- Stub code, TODOs, or placeholders left in
- Type errors, broken imports, syntax issues
- Files claimed to be written but not actually written
- For non-trivial project/code/repo work: evidence that Claude Code used the V4/MCP
  routing contract appropriately (knowledge MCP/get_playbook when available,
  target AGENTS.md/PROJECT_STATE.md when present, relevant toolkit selection,
  and concrete verification before stopping)

If there are issues, use:
VERDICT: ISSUES
1. <specific, actionable fix>
2. <specific, actionable fix>

Do not nitpick style. Only block for concrete misses.

Current task transcript:
---
{transcript}
---
"""

FORMAT_REPAIR_PROMPT = """Your previous audit response did not start with the required verdict line.
Return ONLY one of these formats, with no preamble and no reasoning:

VERDICT: PASS

or:

VERDICT: ISSUES
1. <specific, actionable issue>

If your previous response contained no concrete actionable issue, return:
VERDICT: PASS

Previous response:
---
{response}
---
"""


# --- Helpers ---------------------------------------------------------------
def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def iteration_key(session_id: str, turn_id: str) -> str:
    return f"{session_id}:{turn_id or 'unknown-turn'}"


def load_iterations(session_id: str, turn_id: str) -> int:
    data = load_json_locked(STATE_FILE, {})
    key = iteration_key(session_id, turn_id)
    return int(data.get(key, 0))


def save_iterations(session_id: str, turn_id: str, count: int) -> None:
    def updater(data: dict) -> dict:
        data[iteration_key(session_id, turn_id)] = count
        # Remove legacy session-wide counters so old failures cannot poison new turns.
        data.pop(session_id, None)
        if len(data) > 50:
            data = dict(list(data.items())[-50:])
        return data

    update_json_locked(STATE_FILE, updater)


def save_verdict(session_id: str, turn_id: str, verdict: str, preview: str = "") -> None:
    def updater(data: dict) -> dict:
        data[session_id] = {
            "turn_id": turn_id,
            "verdict": verdict,
            "preview": preview[:500],
            "updated_at": time.time(),
        }
        if len(data) > 50:
            data = dict(list(data.items())[-50:])
        return data

    update_json_locked(VERDICT_FILE, updater)

def redact_sensitive(text: str) -> str:
    api_key = load_api_key()
    if api_key:
        text = text.replace(api_key, "<redacted>")
    text = re.sub(r'tp-[A-Za-z0-9]+', 'tp-<redacted>', text)
    text = re.sub(
        r'"encrypted_content":\s*"[^"]+"',
        '"encrypted_content": "<omitted>"',
        text,
    )
    text = re.sub(
        r'<hook_prompt\b[^>]*>.*?</hook_prompt>',
        '<hook_prompt omitted>',
        text,
        flags=re.DOTALL,
    )
    text = text.replace("<hook_prompt", "<hook-prompt")
    return text


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def compact(text: str, limit: int) -> str:
    text = redact_sensitive(text)
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[omitted]...\n" + text[-limit // 2 :]


def read_task_slice(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(errors="replace").splitlines()
    except Exception:
        return ""

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
            role = payload.get("role") or "message"
            text = content_text(payload.get("content"))
            if role == "user" and text.lstrip().startswith("<hook_prompt"):
                continue
            if text:
                entries.append((role.upper(), text))
        elif item_type == "function_call":
            args = compact(str(payload.get("arguments") or ""), 800)
            entries.append(("TOOL CALL", f"{payload.get('name')}: {args}"))
        elif item_type == "function_call_output":
            output = str(payload.get("output") or "")
            entries.append(("TOOL OUTPUT", compact(output, 1_000)))

    if not entries:
        return ""

    start = 0
    for idx in range(len(entries) - 1, -1, -1):
        if entries[idx][0] == "USER":
            start = idx
            break

    task_entries = entries[start:]
    text = "\n\n".join(f"{role}:\n{body}" for role, body in task_entries)
    text = redact_sensitive(text)
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    first_role, first_body = task_entries[0]
    first = redact_sensitive(f"{first_role}:\n{first_body}")
    marker = "\n\n...[older task details omitted to stay focused]...\n\n"
    tail_budget = max(0, MAX_TRANSCRIPT_CHARS - len(first) - len(marker))
    return first + marker + text[-tail_budget:]


def load_api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV)
    if api_key:
        return api_key

    if KEY_FILE.exists():
        try:
            return KEY_FILE.read_text().strip()
        except Exception:
            pass

    # Claude Code hooks may not inherit interactive shell env. Read the single
    # assignment without sourcing arbitrary shell code.
    for rc_file in (".zshrc", ".zprofile", ".profile"):
        path = Path.home() / rc_file
        if not path.exists():
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("export "):
                stripped = stripped[len("export "):].strip()
            if not stripped.startswith(f"{API_KEY_ENV}="):
                continue
            raw_value = stripped.split("=", 1)[1].strip()
            try:
                parts = shlex.split(raw_value, comments=False, posix=True)
            except ValueError:
                parts = [raw_value.strip("\"'")]
            return parts[0] if parts else ""
    return ""


def allow_stop(reason: str = "") -> None:
    log(f"ALLOW: {reason}")
    sys.exit(0)


def block_stop(reason: str) -> None:
    log(f"BLOCK ({len(reason)} chars): {reason[:160].replace(chr(10), ' ')}...")
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.stdout.flush()
    sys.exit(0)


def normalize_verdict(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("VERDICT: PASS") or stripped.startswith("VERDICT: ISSUES"):
        return stripped
    match = re.search(r"(?m)^VERDICT:\s*(PASS|ISSUES)\b", stripped)
    if not match:
        return stripped
    return stripped[match.start():].strip()


def has_tool_activity(transcript: str) -> bool:
    return any(marker in transcript for marker in TOOL_ACTIVITY_MARKERS)


def ask_mimo(api_key: str, transcript: str, max_tokens: int = 2500) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": "Output only the required VERDICT format. Do not think aloud.",
            },
            {
                "role": "user",
                "content": AUDIT_PROMPT.format(transcript=transcript),
            },
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=AUDIT_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = data["choices"][0]
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    if content:
        return normalize_verdict(content)
    reasoning_content = (message.get("reasoning_content") or "").strip()
    if reasoning_content:
        normalized = normalize_verdict(reasoning_content)
        if normalized.startswith("VERDICT: PASS") or normalized.startswith("VERDICT: ISSUES"):
            log("EMPTY_CONTENT_REASONING_VERDICT_USED")
            return normalized
        log(
            "EMPTY_CONTENT_REASONING_IGNORED: "
            f"finish={choice.get('finish_reason')} "
            f"reasoning_preview={reasoning_content[:160].replace(chr(10), ' ')}"
        )
        return ""
    log(f"EMPTY_RESPONSE: finish={choice.get('finish_reason')} keys={list(message.keys())}")
    return ""


def ask_mimo_with_retry(api_key: str, transcript: str, max_tokens: int = 2500) -> str:
    for attempt in range(1, AUDIT_MAX_ATTEMPTS + 1):
        try:
            return ask_mimo(api_key, transcript, max_tokens=max_tokens)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_HTTP_CODES or attempt >= AUDIT_MAX_ATTEMPTS:
                raise
            log(f"RETRY_HTTP: attempt {attempt}/{AUDIT_MAX_ATTEMPTS} code={exc.code}")
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            if attempt >= AUDIT_MAX_ATTEMPTS:
                raise
            log(f"RETRY_NETWORK: attempt {attempt}/{AUDIT_MAX_ATTEMPTS} error={exc}")
        time.sleep(AUDIT_RETRY_DELAY_S)
    return ""


def repair_verdict_format(api_key: str, response: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 500,
        "temperature": 0.0,
        "messages": [
            {
                "role": "system",
                "content": "You are a strict formatter. Output only the requested verdict format.",
            },
            {
                "role": "user",
                "content": FORMAT_REPAIR_PROMPT.format(response=response[:2500]),
            },
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=AUDIT_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    message = (data["choices"][0].get("message") or {})
    return normalize_verdict((message.get("content") or "").strip())


# --- Main ------------------------------------------------------------------
def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        allow_stop(f"bad payload: {e}")
        return

    # Normalize Claude payload → fills in synthesized turn_id and last_assistant_message.
    payload = normalize_payload(payload)
    session_id = payload.get("session_id") or "unknown"
    turn_id = payload.get("turn_id") or ""
    transcript_path = payload.get("transcript_path") or ""
    last_msg = payload.get("last_assistant_message") or ""

    iterations = load_iterations(session_id, turn_id)
    if iterations >= MAX_ITERATIONS:
        save_verdict(session_id, turn_id, "MAX_ALLOW", f"max iterations reached ({iterations}/{MAX_ITERATIONS})")
        allow_stop(f"max iterations reached ({iterations}/{MAX_ITERATIONS})")
        return

    # Prefer Claude's transcript JSONL parser; fall back to last_assistant_message.
    transcript = latest_task_slice_claude(transcript_path, last_msg, MAX_TRANSCRIPT_CHARS) or redact_sensitive(last_msg)
    log(f"TASK_SLICE: {len(transcript)} chars")
    if len(transcript) < MIN_TRANSCRIPT_CHARS and not has_tool_activity(transcript):
        save_verdict(session_id, turn_id, "SKIP_TOO_SHORT", f"transcript too short ({len(transcript)} chars)")
        allow_stop(f"transcript too short ({len(transcript)} chars)")
        return

    api_key = load_api_key()
    if not api_key:
        save_verdict(session_id, turn_id, "SKIP_NO_KEY", f"{API_KEY_ENV} not set")
        allow_stop(f"{API_KEY_ENV} not set")
        return

    try:
        verdict_text = ask_mimo_with_retry(api_key, transcript)
        if not verdict_text:
            retry_transcript = compact(transcript, 3_000)
            log(f"RETRY_EMPTY: {len(retry_transcript)} chars")
            verdict_text = ask_mimo_with_retry(api_key, retry_transcript, max_tokens=800)
    except urllib.error.HTTPError as e:
        body_preview = e.read().decode("utf-8", errors="replace")[:300]
        save_verdict(session_id, turn_id, "ERROR", f"audit HTTP {e.code}: {body_preview}")
        allow_stop(f"audit HTTP {e.code}: {body_preview}")
        return
    except Exception as e:
        save_verdict(session_id, turn_id, "ERROR", f"audit API error: {e}")
        allow_stop(f"audit API error: {e}")
        return

    log(f"MiMo verdict (iter {iterations + 1}): {verdict_text[:240]}")

    if verdict_text and not (
        verdict_text.startswith("VERDICT: PASS")
        or verdict_text.startswith("VERDICT: ISSUES")
    ):
        try:
            repaired = repair_verdict_format(api_key, verdict_text)
            if repaired.startswith("VERDICT: PASS") or repaired.startswith("VERDICT: ISSUES"):
                log(f"FORMAT_REPAIR_VERDICT: {repaired[:160].replace(chr(10), ' ')}")
                verdict_text = repaired
            else:
                log(f"FORMAT_REPAIR_FAILED: {repaired[:160].replace(chr(10), ' ')}")
        except Exception as exc:
            log(f"FORMAT_REPAIR_ERROR: {exc}")
        if not (verdict_text.startswith("VERDICT: PASS") or verdict_text.startswith("VERDICT: ISSUES")):
            verdict_text = (
                "VERDICT: ISSUES\n"
                "1. MiMo returned a nonstandard audit instead of the required "
                "PASS/ISSUES format after a repair retry. Treat the raw review "
                "below as blocking feedback and address any concrete misses before stopping.\n\n"
                f"Raw MiMo review:\n{verdict_text[:1800]}"
            )
            log("NORMALIZED_NONSTANDARD_VERDICT_TO_ISSUES")

    if verdict_text.startswith("VERDICT: PASS"):
        save_verdict(session_id, turn_id, "PASS", verdict_text)
        allow_stop("audit passed")
        return

    if verdict_text.startswith("VERDICT: ISSUES"):
        save_iterations(session_id, turn_id, iterations + 1)
        save_verdict(session_id, turn_id, "ISSUES", verdict_text)
        record_lesson("mimo_stop", verdict_text.splitlines()[1] if len(verdict_text.splitlines()) > 1 else verdict_text)
        feedback = (
            f"[MiMo code-review hook — iteration "
            f"{iterations + 1}/{MAX_ITERATIONS}]\n\n"
            f"A second model (MiMo-v2.5-Pro) reviewed your work and found issues. "
            f"Fix these before stopping:\n\n"
            f"{verdict_text}\n\n"
            f"After addressing each item, run any relevant checks and continue."
        )
        block_stop(feedback)
        return

    save_iterations(session_id, turn_id, iterations + 1)
    save_verdict(session_id, turn_id, "UNPARSEABLE", verdict_text)
    feedback = (
        f"[MiMo code-review hook — iteration "
        f"{iterations + 1}/{MAX_ITERATIONS}]\n\n"
        f"MiMo did not return a parseable PASS/ISSUES verdict. Treat this as "
        f"an audit failure, re-check the user's request, verify the work with "
        f"strong relevant tests/logs, and only stop once the audit passes.\n\n"
        f"Raw verdict preview: {verdict_text[:120]!r}"
    )
    block_stop(feedback)


if __name__ == "__main__":
    main()

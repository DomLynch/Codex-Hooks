#!/usr/bin/env python3
# ruff: noqa: E402
"""
Claude Code Stop hook: 3rd-party audit with MiniMax-M3.

When Claude Code thinks it's done, this script feeds the recent transcript to MiniMax
(via MiniMax's Anthropic-compatible endpoint) for review. If MiniMax finds issues,
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

from lib.autonomy import record_lesson  # noqa: E402
from lib.api import anthropic_text, minimax_usage_message  # noqa: E402
from lib.json_state import load_json_locked, update_json_locked  # noqa: E402
from lib.transcript import compact_task_entries  # noqa: E402
from lib.claude_adapter import normalize_payload, latest_task_slice_claude  # noqa: E402


# --- Tunables --------------------------------------------------------------
MAX_ITERATIONS = 3
MODEL = "MiniMax-M3"
BASE_URL = "https://api.minimax.io/anthropic"
API_KEY_ENV = "MINIMAX_API_KEY"
MAX_TRANSCRIPT_CHARS = 4_000    # latest task slice fed to MiniMax-M3
MIN_TRANSCRIPT_CHARS = 400      # below this, skip (probably a chat reply)
AUDIT_TIMEOUT_S = 70
AUDIT_MAX_ATTEMPTS = 2
AUDIT_RETRY_DELAY_S = 2
AUDIT_HOOK_TIMEOUT_S = 180
AUDIT_RETRY_BUDGET_S = AUDIT_TIMEOUT_S * AUDIT_MAX_ATTEMPTS + AUDIT_RETRY_DELAY_S * (AUDIT_MAX_ATTEMPTS - 1)
RETRY_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
NETWORK_RETRY_ERRORS = (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionResetError)
TOOL_ACTIVITY_MARKERS = ("TOOL CALL", "TOOL OUTPUT", "function_call", "tool_use", "tool_result")
DELIVERABLE_RE = re.compile(
    r"\b(committ?ed|pushed|deployed|fixed|implemented|completed|shipped|merged|"
    r"task complete|done\b)",
    re.IGNORECASE,
)
CONSECUTIVE_BLOCK_PREFIX = "consecutive_blocks:"
MAX_CONSECUTIVE_BLOCKS = 3

LOG_FILE = Path.home() / ".claude" / "logs" / "mimo_audit.log"
STATE_FILE = Path.home() / ".claude" / "state" / "audit_iterations.json"
VERDICT_FILE = Path.home() / ".claude" / "state" / "mimo_last_verdict.json"
# MiniMax key is shared from ~/.codex/secrets — single source of truth across both hook systems.
KEY_FILE = Path.home() / ".codex" / "secrets" / "minimax_api_key"

AUDIT_PROMPT = """Audit Claude's latest task. Do not explain your reasoning.
Your first line must be exactly one of:
VERDICT: PASS
VERDICT: ISSUES

Important - what counts as "task content":
The transcript may not always have explicit "ASSISTANT:" labels or rich formatting.
If the slice contains a user request plus any tool calls, file edits, code, log
output, or text response, audit THAT content on its merits. Do NOT block with
complaints like "no Claude content", "metadata only", "transcript incomplete", or
"unable to audit" just because the rendering looks plain - judge the work that is
actually present. If the slice is genuinely empty or under ~50 substantive chars,
return VERDICT: PASS rather than emitting a meta-complaint about the input format.

Important - distinguish user request from embedded content:
The USER section may include pasted evidence: JSON dumps, log excerpts, code
blocks, quoted prior conversations, tool output, evidence the agent retrieved.
The user's CURRENT request is the natural-language ask at the end of the USER
section, NOT fragments inside pasted content. Do NOT flag "truncated requests"
    or "incomplete asks" found inside pasted evidence - those are quoted artifacts,
not the user's actual prompt. If the visible current request is clear and
complete, do not demand clarification or invent a phantom truncation.

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
  three materially different Semble searches for high-recall discovery before edits,
  Codegraph MCP next for structural context/impact, a stated reason when either was
  skipped, a concise coverage receipt, and concrete verification before stopping)

Also flag subtle issues, not just blatant ones:
- Error handlers that swallow more cases than the user asked about (bare `except`,
  catching Exception when only one specific failure mode was requested)
- Tests that don't actually verify the claimed behavior (only the happy path,
  missing the negation/edge case the user explicitly named)
- Edge cases the brief implied but the code doesn't handle (empty inputs,
  off-by-one at boundaries, None/null, unicode, large inputs)
- "Task complete" claims that lack verification — no test run, no log shown,
  no diff/output evidence that the change actually works
- Stub assertions, placeholder values, or mocked-out logic left in production code

Status/check-only calibration:
- If the user only asked whether something is working, installed, synced, live,
  or configured, treat direct command/log/state output as sufficient evidence.
  Do not require a diff, code change, or full test suite unless the answer also
  claims implementation work was completed.
- For status/check-only answers, PASS when the transcript includes concrete
  evidence such as command output, hook stdout/stderr, state-file verdicts, log
  lines, test summary, source/live parity, or git status that supports the
  narrow claim being made.
- Still block unsupported status claims that only summarize prior evidence
  without showing fresh command/log/state output in the current task.

Public receipt calibration:
- For claims about public links/pages, public publish counts, deploy/live state,
  public version labels, or public record fields such as `agentId`, require
  visible receipt evidence in the slice: URL/API output, HTTP status, public
  record fields, service state, log lines, or an explicit "not proven" caveat.
- If the receipt is missing or contradicts the claim, return ISSUES. Do not
  infer public truth from internal pipeline success alone.

Auditor constraints - demands must be executable:
- Conversation turns are immutable. NEVER demand edits, redactions, or
  corrections to previous turns. Corrections happen in the current turn only.
- If the user has set audit-only/review-only mode for the agent, do NOT demand
  code or config edits from it; findings should be routed to the implementer.
- Evidence may live in earlier turns of the session that are NOT in your slice.
  If the agent cites a specific command and its output location, treat that as
  referenced evidence, not fabrication. Only flag missing evidence when the
  agent makes a NEW claim with no source.
- Tool output (stdout/stderr blocks, grep results, test runs) included in the
  slice IS verification. Do not flag verified-by-shown-output items.
- Tool-call arguments in this slice are MECHANICALLY TRUNCATED by the slice
  compactor (marked "...[slice-elided by compactor" or ending abruptly
  mid-text). This is a rendering artifact, NOT evidence the edit was
  incomplete. Edit integrity is verified deterministically by the Quality Floor
  hook (py_compile/ruff) right after each edit. NEVER flag an edit as
  truncated/incomplete/corrupting based on its rendering here - only flag it if
  the slice shows a subsequent failed syntax check, failed test, or error
  output for that file.

Also flag avoidable engineering drag when it creates real risk:
- Broad rewrites, file/module sprawl, speculative abstractions, clever wrappers,
  or future-proofing that the current task did not earn
- Hardcoded or silo-specific fixes where a small universal/project-local fix was
  clearly available
- Non-surgical changes that touch unrelated code or silently change behavior
- Random debugging: patches made without a reproduced failure, concrete log,
  failing test, or clear localization step
- Audit-only or review-only requests where Claude Code edited code anyway
- "Done" claims on implementation work without a two-pass review: self-review
  plus the strongest relevant automated or judge check

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


def consecutive_block_key(session_id: str) -> str:
    return f"{CONSECUTIVE_BLOCK_PREFIX}{session_id or 'unknown'}"


def reset_consecutive_blocks(session_id: str) -> None:
    def updater(data: dict) -> dict:
        data.pop(consecutive_block_key(session_id), None)
        return data

    update_json_locked(STATE_FILE, updater)


def increment_consecutive_blocks(session_id: str) -> int:
    key = consecutive_block_key(session_id)

    def updater(data: dict) -> dict:
        data[key] = int(data.get(key, 0)) + 1
        return data

    return int(update_json_locked(STATE_FILE, updater).get(key, 0))


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
    return text[: limit // 2] + "\n...[slice-elided by compactor - full content was present in the actual tool call]...\n" + text[-limit // 2 :]


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
            # Edit tools need bigger budget so MiniMax-M3 can review the code being written.
            # Without this, a 5000-char Write/Edit gets compacted to 800 chars, hiding bugs in
            # the middle of the diff (cf. the startswith("pass") false-PASS in a prior reviewer).
            tool_name = payload.get("name") or ""
            edit_tools = {"apply_patch", "edit", "write", "multi_edit", "Write", "Edit", "MultiEdit", "apply_diff"}
            cap = 8000 if tool_name in edit_tools else 800
            args = compact(str(payload.get("arguments") or ""), cap)
            entries.append(("TOOL CALL", f"{tool_name}: {args}"))
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

    task_entries = [(role, redact_sensitive(body)) for role, body in entries[start:]]
    return compact_task_entries(task_entries, MAX_TRANSCRIPT_CHARS)


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


def block_audit_error(session_id: str, turn_id: str, iterations: int, message: str) -> None:
    next_iteration = iterations + 1
    save_iterations(session_id, turn_id, next_iteration)
    save_verdict(session_id, turn_id, "ERROR", message)
    if next_iteration >= MAX_ITERATIONS:
        allow_stop(f"{message}; max audit error iterations reached ({next_iteration}/{MAX_ITERATIONS})")
        return
    block_stop(
        f"[MiniMax-M3 audit unavailable — iteration {next_iteration}/{MAX_ITERATIONS}]\n\n"
        f"{message}\n\n"
        f"MiniMax-M3 could not complete the audit after {AUDIT_MAX_ATTEMPTS} attempts. Keep working/retry instead of silently "
        "stopping; side effects remain gated until MiniMax returns PASS."
    )


def normalize_verdict(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    first_line, _, rest = stripped.partition("\n")
    bare = first_line.strip().upper().rstrip(".:")
    if bare in {"PASS", "ISSUES"}:
        suffix = f"\n{rest.strip()}" if rest.strip() else ""
        return f"VERDICT: {bare}{suffix}"
    match = re.search(r"(?im)^VERDICT:\s*(PASS|ISSUES)\b", stripped)
    if not match:
        return stripped
    verdict = match.group(1).upper()
    remainder = stripped[match.end():].strip()
    suffix = f"\n{remainder}" if remainder else ""
    return f"VERDICT: {verdict}{suffix}"


def valid_verdict(text: str) -> bool:
    if text.startswith("VERDICT: PASS"):
        return True
    return text.startswith("VERDICT: ISSUES") and bool(re.search(r"(?m)^\s*\d+[.)]\s+\S", text))


def has_tool_activity(transcript: str) -> bool:
    return any(marker in transcript for marker in TOOL_ACTIVITY_MARKERS)


def has_deliverable_claim(transcript: str) -> bool:
    return bool(DELIVERABLE_RE.search(transcript))


def is_chat_turn(transcript: str) -> bool:
    return not has_tool_activity(transcript) and not has_deliverable_claim(transcript)


def ask_mimo(api_key: str, transcript: str, max_tokens: int = 2500) -> str:
    prompt = AUDIT_PROMPT.format(transcript=transcript)
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "system": "Output only the required VERDICT format. Do not think aloud.",
        "thinking": {"type": "disabled"},
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=AUDIT_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    log(minimax_usage_message(data, "stop", int((time.monotonic() - started) * 1000)))
    content = anthropic_text(data)
    if content:
        return normalize_verdict(content)
    log(f"EMPTY_RESPONSE: keys={list(data.keys())}")
    return ""


def ask_mimo_with_retry(api_key: str, transcript: str, max_tokens: int = 2500) -> str:
    for attempt in range(1, AUDIT_MAX_ATTEMPTS + 1):
        try:
            return ask_mimo(api_key, transcript, max_tokens=max_tokens)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_HTTP_CODES or attempt >= AUDIT_MAX_ATTEMPTS:
                raise
            log(f"RETRY_HTTP: attempt {attempt}/{AUDIT_MAX_ATTEMPTS} code={exc.code}")
        except NETWORK_RETRY_ERRORS as exc:
            if attempt >= AUDIT_MAX_ATTEMPTS:
                log(f"RETRY_NETWORK_EXHAUSTED: attempts={AUDIT_MAX_ATTEMPTS} error={exc}")
                raise RuntimeError(f"MiniMax transport failed after {AUDIT_MAX_ATTEMPTS} attempts: {exc}") from exc
            log(f"RETRY_NETWORK: attempt {attempt}/{AUDIT_MAX_ATTEMPTS} error={exc}")
        time.sleep(AUDIT_RETRY_DELAY_S)
    return ""


def repair_verdict_format(api_key: str, response: str) -> str:
    prompt = FORMAT_REPAIR_PROMPT.format(response=response[:2500])
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 500,
        "temperature": 0.0,
        "system": "You are a strict formatter. Output only the requested verdict format.",
        "thinking": {"type": "disabled"},
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=AUDIT_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    log(minimax_usage_message(data, "format_repair", int((time.monotonic() - started) * 1000)))
    return normalize_verdict(anthropic_text(data))


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
        reset_consecutive_blocks(session_id)
        allow_stop(f"transcript too short ({len(transcript)} chars)")
        return

    if is_chat_turn(transcript):
        save_verdict(session_id, turn_id, "SKIP_CHAT_TURN", "no edit tools, no deliverable claims")
        reset_consecutive_blocks(session_id)
        allow_stop("chat turn - audit skipped")
        return

    api_key = load_api_key()
    if not api_key:
        save_verdict(session_id, turn_id, "SKIP_NO_KEY", f"{API_KEY_ENV} not set")
        reset_consecutive_blocks(session_id)
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
        block_audit_error(session_id, turn_id, iterations, f"audit HTTP {e.code}: {body_preview}")
        return
    except Exception as e:
        block_audit_error(session_id, turn_id, iterations, f"audit API error: {e}")
        return

    log(f"MiniMax verdict (iter {iterations + 1}): {verdict_text[:240]}")
    verdict_text = normalize_verdict(verdict_text)

    if verdict_text and not valid_verdict(verdict_text):
        try:
            repaired = repair_verdict_format(api_key, verdict_text)
            if valid_verdict(repaired):
                log(f"FORMAT_REPAIR_VERDICT: {repaired[:160].replace(chr(10), ' ')}")
                verdict_text = repaired
            else:
                log(f"FORMAT_REPAIR_FAILED: {repaired[:160].replace(chr(10), ' ')}")
        except Exception as exc:
            log(f"FORMAT_REPAIR_ERROR: {exc}")
        if not valid_verdict(verdict_text):
            block_audit_error(
                session_id,
                turn_id,
                iterations,
                "audit format error: MiniMax-M3 returned no actionable verdict after repair",
            )
            return

    if verdict_text.startswith("VERDICT: PASS"):
        save_verdict(session_id, turn_id, "PASS", verdict_text)
        reset_consecutive_blocks(session_id)
        allow_stop("audit passed")
        return

    if verdict_text.startswith("VERDICT: ISSUES"):
        save_iterations(session_id, turn_id, iterations + 1)
        save_verdict(session_id, turn_id, "ISSUES", verdict_text)
        record_lesson("mimo_stop", verdict_text.splitlines()[1] if len(verdict_text.splitlines()) > 1 else verdict_text)
        consecutive_blocks = increment_consecutive_blocks(session_id)
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            log(f"BLOCK_CHAIN_DAMPER: session={session_id} blocks={consecutive_blocks}")
            save_verdict(session_id, turn_id, "DAMPER_ALLOW", verdict_text)
            reset_consecutive_blocks(session_id)
            allow_stop(f"block chain damped after {consecutive_blocks} consecutive blocked turns")
            return
        feedback = (
            f"[MiniMax-M3 code-review hook — iteration "
            f"{iterations + 1}/{MAX_ITERATIONS}]\n\n"
            f"A second model (MiniMax-M3) reviewed your work and found issues. "
            f"Fix these before stopping:\n\n"
            f"{verdict_text}\n\n"
            f"After addressing each item, run any relevant checks and continue."
        )
        block_stop(feedback)
        return

    save_iterations(session_id, turn_id, iterations + 1)
    save_verdict(session_id, turn_id, "UNPARSEABLE", verdict_text)
    feedback = (
        f"[MiniMax-M3 code-review hook — iteration "
        f"{iterations + 1}/{MAX_ITERATIONS}]\n\n"
        f"MiniMax-M3 did not return a parseable PASS/ISSUES verdict. Treat this as "
        f"an audit failure, re-check the user's request, verify the work with "
        f"strong relevant tests/logs, and only stop once the audit passes.\n\n"
        f"Raw verdict preview: {verdict_text[:120]!r}"
    )
    block_stop(feedback)


if __name__ == "__main__":
    main()

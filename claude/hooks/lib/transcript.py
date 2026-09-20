from __future__ import annotations

import json
import re
from pathlib import Path


MAX_TRANSCRIPT_CHARS = 4000


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
    text = re.sub(r"<hook_prompt\b[^>]*>.*?</hook_prompt>", "<hook_prompt omitted>", text, flags=re.DOTALL)
    return text.replace("<hook_prompt", "<hook-prompt")


def strip_hook_prompts(text: str) -> str:
    return re.sub(r"<hook_prompt\b[^>]*>.*?</hook_prompt>", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()


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


TEST_RUNNER_RE = re.compile(
    r"\b(pytest|unittest|npm (run )?(test|quality|check|lint)|node --test|vitest|jest|"
    r"cargo test|go test|make (test|check)|ruff check|mypy|tsc\b|\.quality-gate)",
    re.IGNORECASE,
)
FAILURE_ACK_RE = re.compile(
    r"\b(fail(s|ed|ing|ure)?|error|broken|red|not passing|unresolved|regression|AssertionError|"
    r"still (fails|failing|broken))\b",
    re.IGNORECASE,
)


def unacknowledged_check_failure(entries: list[tuple[str, str]], final_message: str) -> str:
    """Deterministic gate for the most common audit finding: a test/check runner failed
    this turn and the final message says nothing about it.

    Only the LAST test-runner output counts (a later green rerun supersedes an earlier
    red one). Returns the failure excerpt to quote back, or "" when nothing to flag.
    """
    last = ""
    for role, body in entries:
        if role == "TOOL OUTPUT" and TEST_RUNNER_RE.search(body.split("\n", 1)[0]):
            last = body
    if not last:
        return ""
    excerpt = _failure_excerpt(last, 300)
    if not excerpt or FAILURE_ACK_RE.search(final_message or ""):
        return ""
    return excerpt


EDIT_TOOL_RE = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit):", re.IGNORECASE)
_FILE_PATH_ARG_RE = re.compile(r'"file_path":\s*"([^"]+)"')
_CONFLICT_OPEN_RE = re.compile(r"^<{7} \S", re.MULTILINE)
_CONFLICT_CLOSE_RE = re.compile(r"^>{7} \S", re.MULTILINE)
# Paths in prose: absolute, relative-with-slash, or bare image/pdf names (the observed failure class).
_PATH_MENTION_RE = re.compile(
    r"(?<![\w/])((?:/|\./|~/)?(?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]{1,6}|[\w.\-]+\.(?:png|jpe?g|gif|webp|svg|pdf))(?![\w/])"
)


def edited_paths(entries: list[tuple[str, str]]) -> list[str]:
    """file_path args from Edit/Write-style TOOL CALLs this turn, in order, deduped."""
    seen: list[str] = []
    for role, body in entries:
        if role == "TOOL CALL" and EDIT_TOOL_RE.match(body.split(":", 1)[0] + ":"):
            for path in _FILE_PATH_ARG_RE.findall(body):
                if path not in seen:
                    seen.append(path)
    return seen


def conflict_marker_files(paths: list[str]) -> list[str]:
    """Files among `paths` that still contain git conflict markers."""
    hits = []
    for raw in paths:
        f = Path(raw).expanduser()
        try:
            if f.is_file() and f.stat().st_size < 5_000_000:
                text = f.read_text(errors="replace")
                if _CONFLICT_OPEN_RE.search(text) and _CONFLICT_CLOSE_RE.search(text):
                    hits.append(raw)
        except OSError:
            continue
    return hits


def unopened_mentioned_files(entries: list[tuple[str, str]], final_message: str, cwd: str) -> list[str]:
    """Paths the final message cites that exist on disk but never appeared in ANY tool call or
    tool output this turn. Appearing in grep/ls output counts as 'seen' (reporting a location is
    not a claim to have read it); only paths that exist solely in the prose are flagged."""
    tool_text = "\n".join(body for role, body in entries if role.startswith("TOOL"))
    base = Path(cwd or ".").expanduser()
    hits: list[str] = []
    for token in _PATH_MENTION_RE.findall(final_message or ""):
        name = Path(token).name
        if token in tool_text or name in tool_text:
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = base / token
        if not candidate.is_file() and "/" not in token:
            # bare image name: resolve via git index when cwd is a repo
            try:
                import subprocess
                out = subprocess.run(["git", "-C", str(base), "ls-files", "--", f"*/{token}", token],
                                     capture_output=True, text=True, timeout=5).stdout.split()
                candidate = (base / out[0]) if out else candidate
            except Exception:
                pass
        if candidate.is_file() and token not in hits:
            hits.append(token)
    return hits


SENSITIVE_PATH_RE = re.compile(
    r"(auth|login|session|token|secret|credential|passw|crypt|payment|billing|stripe|checkout|"
    r"admin|permission|rbac|oauth|jwt|api[_-]?key|\bacl\b)",
    re.IGNORECASE,
)
_WARNING_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")


def sensitive_paths(entries: list[tuple[str, str]]) -> list[str]:
    """Edited paths this turn that touch auth/secrets/payment/admin surfaces."""
    return [p for p in edited_paths(entries) if SENSITIVE_PATH_RE.search(p)]


def sensitive_section(paths: list[str]) -> str:
    return (
        "SENSITIVE PATHS EDITED THIS TURN (auth / secrets / payment / admin):\n"
        + "\n".join(f"- {p}" for p in paths)
        + "\nApply a security-review standard to these. Any concern about authentication, "
        "authorization, secret handling, injection, or data exposure is BLOCKING — return "
        "VERDICT: ISSUES for it, never PASS with a warning."
    )


def pass_warnings(verdict_text: str) -> list[str]:
    """Bullet/numbered lines after a PASS verdict — the reviewer's 'optional Warnings'."""
    if not (verdict_text or "").startswith("VERDICT: PASS"):
        return []
    rest = verdict_text.split("\n", 1)[1] if "\n" in verdict_text else ""
    return [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip()
            for line in rest.splitlines() if _WARNING_LINE_RE.match(line)]


DOC_EXT_RE = re.compile(r"\.(md|mdx|markdown|txt|rst|adoc|asciidoc)$", re.IGNORECASE)
_MUTATING_BASH_RE = re.compile(
    r"(\bsed\s+-i|\btee\b|(?<![<>])>(?!>)|>>|\bmv\b|\bcp\b|\brm\b|\bgit\s+(commit|push|add|checkout|reset|rebase|merge)\b|"
    r"\bnpm\b|\bpnpm\b|\byarn\b|\bpip\b|\bpoetry\b|\bmake\b|\bcargo\b|\bgo\s+(build|run|install)\b|\bpython3?\s+\S+\.py)",
    re.IGNORECASE,
)


def doc_only_turn(entries: list[tuple[str, str]], final_message: str, deliverable_re: re.Pattern) -> bool:
    """True when the turn only edited documentation and made no shipping claim.

    Conservative: any Edit/Write on a non-doc file, any Bash call that looks like it
    mutates the tree or runs code, or any push/deploy/done claim disqualifies. A wrong
    answer here only costs one ordinary audit, so bias toward False."""
    edited = edited_paths(entries)
    if not edited or not all(DOC_EXT_RE.search(p) for p in edited):
        return False
    for role, body in entries:
        if role == "TOOL CALL" and body.startswith("Bash:") and _MUTATING_BASH_RE.search(body):
            return False
    return not deliverable_re.search(final_message or "")


DIFF_CAP = 2500


def turn_diff(cwd: str, paths: list[str], cap: int = DIFF_CAP) -> str:
    """`git diff HEAD` restricted to the files this turn edited, plus the head of any
    new untracked ones. Scoped so another agent's changes in a shared worktree are not
    attributed to this turn. Empty string when not a repo / nothing to show."""
    if not cwd or not paths:
        return ""
    import subprocess

    base = Path(cwd).expanduser()
    if not (base / ".git").exists() and subprocess.run(["git", "-C", str(base), "rev-parse", "--git-dir"],
                                                       capture_output=True, timeout=5).returncode:
        return ""
    rel = []
    for raw in paths:
        f = Path(raw).expanduser()
        try:
            rel.append(str(f.resolve().relative_to(base.resolve())) if f.is_absolute() else raw)
        except ValueError:
            continue  # outside the repo
    if not rel:
        return ""
    parts = []
    try:
        d = subprocess.run(["git", "-C", str(base), "diff", "HEAD", "--", *rel], capture_output=True, text=True, timeout=10)
        if d.returncode and "unknown revision" in (d.stderr or ""):   # fresh repo, no HEAD yet
            d = subprocess.run(["git", "-C", str(base), "diff", "--", *rel], capture_output=True, text=True, timeout=10)
        if d.stdout.strip():
            parts.append(d.stdout.strip())
        u = subprocess.run(["git", "-C", str(base), "ls-files", "--others", "--exclude-standard", "--", *rel],
                           capture_output=True, text=True, timeout=5).stdout.split()
        for name in u[:5]:
            try:
                body = (base / name).read_text(errors="replace")
                parts.append(f"--- NEW FILE {name} ---\n{body[:800]}")
            except OSError:
                continue
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return _bounded_compact("\n\n".join(parts), cap, "\n...[diff elided to fit]...\n") if parts else ""


# Narrow by design: an explicit HTTP status claim about a named host is the only live/deploy
# claim shape a regex can separate from narrative. Broad "is live / shipped / deployed" matching
# fired on 89% of real turns in replay (commit links, "live API", "shipped 30%", quoted SHAs).
_HTTP_CLAIM_RE = re.compile(
    r"(?:\b(?:HTTP/?(?:1\.1|2)?\s*|returns?\s+(?:a\s+)?|responds?\s+(?:with\s+)?|status\s+(?:code\s+)?|got\s+(?:a\s+)?)|→\s*|=\s*)(2\d\d)\b(?:\s*OK)?",
    re.IGNORECASE,
)
_HTTP_CLAIM_NEG_RE = re.compile(
    r"\b(?:not|isn't|no|never|failed|instead of|expected|expecting|should|want|until|before)\b(?:[^.\n]|\.(?=\w)){0,40}\b2\d\d\b"
    r"|\b2\d\d\b(?:[^.\n]|\.(?=\w)){0,25}\b(?:not|isn't|expected|but got|instead|yet)\b",
    re.IGNORECASE,
)
# Lowercase only: hostnames in prose are lowercase; "OSF.IO/Q4DJS" inside a DOI is not a claim about
# osf.io. No `.app`: `agent.app` / `src.app` are Python modules far more often than hosts.
_HOST_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|co\.uk|ae))\b")
_HOST_IGNORE = {"github.com", "example.com", "localhost.com"}
# A 2xx written under a heading like "Acceptance criteria" / "Deploy + verify:", or as the expected
# result of a backticked command (`curl X` → 200), is a plan or a checklist, not a report.
_PLAN_CONTEXT_RE = re.compile(r"acceptance criteria|verify:|to verify|checklist|next steps?\b|\bto-?do\b|\brun:", re.IGNORECASE)
_EXPECTED_RESULT_RE = re.compile(r"`[^`\n]*\b(?:curl|wget|http|fetch)\b[^`\n]*`\s*(?:→|->|=>|:|=)\s*$")
_HTTP_OK_RE = re.compile(r"\bHTTP/\S+\s+2\d\d\b|(?<![\d.])2\d\d(?![\d.])", re.MULTILINE)
_HTTP_FAIL_RE = re.compile(r"curl: \(\d+\)|Connection refused|Could not resolve|Failed to connect|timed out", re.IGNORECASE)
_CALL_ID_RE = re.compile(r"\[(toolu_[A-Za-z0-9]+|call_[A-Za-z0-9]+|[a-z]\w{0,12})\]")
_ELIDED_MARKER = "slice-elided by compactor"


def unreceipted_live_claim(entries: list[tuple[str, str]], final_message: str) -> str:
    """Final message asserts an HTTP 2xx from a named host, and no TOOL OUTPUT this turn shows a
    successful response naming that host. Returns the hosts lacking a receipt, or "".

    Fires only on positive evidence of absence. A paired call that names the host but whose output
    was elided by the compactor is indeterminate and stays silent (left to the model)."""
    msg = final_message or ""
    if _HTTP_CLAIM_NEG_RE.search(msg):
        return ""
    # Only hosts named within ~100 chars of a 2xx claim are being claimed about. A long
    # message can mention many hosts; demanding receipts for all of them fired on real
    # audit turns that had curled exactly the host they reported.
    hosts: set[str] = set()
    for m in _HTTP_CLAIM_RE.finditer(msg):
        line_start = msg.rfind("\n", 0, m.start()) + 1
        if _PLAN_CONTEXT_RE.search(msg[max(0, m.start() - 200): m.start()]) or _EXPECTED_RESULT_RE.search(msg[line_start: m.start()]):
            continue
        window = msg[max(0, m.start() - 100): m.end() + 100]
        hosts |= {h.group(1) for h in _HOST_RE.finditer(window)}
    hosts -= _HOST_IGNORE
    if not hosts:
        return ""
    # Pair calls with outputs by call_id. The host is matched against the CALL too (800-char cap,
    # URL near the front) because the output embeds only a 300-char copy of the command and a
    # `cd x && echo "..." && curl URL` prefix pushes the URL into the elided middle. Success is
    # still judged from the OUTPUT, so a failed call never counts as a receipt.
    calls = {m.group(1): body for role, body in entries if role == "TOOL CALL"
             for m in [_CALL_ID_RE.search(body)] if m}
    candidates: list[tuple[str, bool, bool, bool]] = []  # (text, ok, failed, elided)
    for role, body in entries:
        if role != "TOOL OUTPUT":
            continue
        m = _CALL_ID_RE.search(body)
        text = ((calls.get(m.group(1), "") if m else "") + "\n" + body).lower()
        out = body.partition("\n")[2]
        candidates.append((text, bool(_HTTP_OK_RE.search(out)), bool(_HTTP_FAIL_RE.search(out)), _ELIDED_MARKER in out))
    missing = []
    for host in sorted(hosts):
        mine = [c for c in candidates if host in c[0]]
        if any(ok and not failed for _, ok, failed, _e in mine):
            continue
        if any(elided or (ok and failed) for _, ok, failed, elided in mine):
            continue  # can't tell from what survived compaction; leave it to the reviewer
        missing.append(host)
    return ", ".join(missing)


def deterministic_findings(entries: list[tuple[str, str]], final_message: str, cwd: str) -> list[str]:
    """All mechanical pre-model gates. Each item is a complete, actionable finding."""
    findings: list[str] = []
    missed = unacknowledged_check_failure(entries, final_message)
    if missed:
        findings.append(
            "A test/check run in this turn failed and your final message does not mention it. "
            "Either fix it and show the rerun, or state the failure and why it is expected:\n" + missed
        )
    conflicts = conflict_marker_files(edited_paths(entries))
    if conflicts:
        findings.append(
            "Files you edited this turn still contain git conflict markers (<<<<<<< / >>>>>>>). "
            "Resolve them and show the clean file: " + ", ".join(conflicts)
        )
    live = unreceipted_live_claim(entries, final_message)
    if live:
        findings.append(
            "Your final message reports an HTTP 2xx from a host, but no tool output this turn shows a successful "
            "response from it. Run the request and include the output, or drop the status claim. Unreceipted: " + live
        )
    unopened = unopened_mentioned_files(entries, final_message, cwd)
    if unopened:
        findings.append(
            "Your final message refers to files that exist but were never opened or produced by any "
            "tool call this turn. Read them and ground the claim, or drop the reference: " + ", ".join(unopened)
        )
    return findings


def compact_task_entries(entries: list[tuple[str, str]], max_chars: int) -> str:
    text = sanitize("\n\n".join(_render_entry(role, body) for role, body in entries))
    if len(text) <= max_chars:
        return text
    failures = [(body, _failure_excerpt(body, 220)) for role, body in entries if role == "TOOL OUTPUT"]
    failures = [(body, excerpt) for body, excerpt in failures if excerpt]
    history = ""
    if failures and max_chars >= 1600:
        history = "\n\nCHECK HISTORY (excerpts, not verdicts; match later reruns and expected outcomes):\n"
        history += "\n".join(_bounded_compact(body.splitlines()[0], 100) + "\n" + excerpt
                             for body, excerpt in failures[-4:])
        history = _bounded_compact(history, min(900, max_chars // 4))
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
    return compact_latest_task(entries, max_chars, include_previous)

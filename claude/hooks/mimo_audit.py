#!/usr/bin/env python3
# ruff: noqa: E402
"""
Claude Code Stop hook: independent audit via Claude CLI (default) or Codex CLI.

When Claude Code thinks it's done, this script feeds the recent transcript to a
fresh ephemeral Codex session. If the reviewer finds issues,
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
import re
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import forget_lesson, hook_env, lesson_lane, record_lesson  # noqa: E402
from lib.api import AUDITOR_NAME, call_mimo, exit_if_audit_child  # noqa: E402
from lib.telemetry import report_failure  # noqa: E402
from lib.policies import is_project_work  # noqa: E402
from lib.audit_state import (  # noqa: E402
    increment_audit_errors, increment_consecutive_blocks, load_iterations, remember_blocked_turn, reset_audit_errors,
    reset_consecutive_blocks, retry_turn_id, save_iterations, save_verdict, write_escalation,
)
from lib.audit_prompt import AUDIT_PROMPT, FORMAT_REPAIR_PROMPT  # noqa: E402
from lib.verdicts import ADVISORY_RE, ELISION_RE, QUOTE_RE, _norm, normalize_verdict, valid_verdict, void_finding  # noqa: E402
from lib.transcript import DIFF_CAP, deterministic_findings, doc_only_turn, edited_paths, pass_warnings, sensitive_paths, sensitive_section, turn_commits, turn_commits_diff, turn_diff  # noqa: E402
from lib import ledger  # noqa: E402
from lib.git import resolve_git_cwd  # noqa: E402
from lib.rehome import is_rehome_turn  # noqa: E402
from lib.handoff_check import handoff_notice  # noqa: E402
from lib.claude_adapter import normalize_payload, latest_task_slice_claude, latest_task_entries_claude, latest_turn_started_at, owner_instructions, peer_triggered_unauthorized_action, earlier_session_evidence  # noqa: E402


# --- Tunables --------------------------------------------------------------
MAX_ITERATIONS = 3
MAX_TRANSCRIPT_CHARS = 4_000    # latest task slice fed to the reviewer
MIN_TRANSCRIPT_CHARS = 400      # below this, skip (probably a chat reply)
# p95 review is ~52s; 70s timed out 24 of 632 reviews in one day. Two attempts must fit the
# Stop hook's timeout (settings: 240s).
AUDIT_TIMEOUT_S = 100
AUDIT_MAX_ATTEMPTS = 2
AUDIT_RETRY_DELAY_S = 2
AUDIT_HOOK_TIMEOUT_S = 240
AUDIT_RETRY_BUDGET_S = AUDIT_TIMEOUT_S * AUDIT_MAX_ATTEMPTS + AUDIT_RETRY_DELAY_S * (AUDIT_MAX_ATTEMPTS - 1)
NETWORK_RETRY_ERRORS = (TimeoutError, ConnectionResetError)
TOOL_ACTIVITY_MARKERS = ("TOOL CALL", "TOOL OUTPUT", "function_call", "tool_use", "tool_result")
DELIVERABLE_RE = re.compile(
    r"\b(committ?ed|pushed|deployed|fixed|implemented|completed|shipped|merged|"
    r"task complete|done\b)",
    re.IGNORECASE,
)
MAX_CONSECUTIVE_AUDIT_ERRORS = 3   # reviewer down this many Stops in a row -> escalate + release
PENDING_WORK_RE = re.compile(
    r"\b(shall I|should I|may I|want me to|would you like me to|can I proceed|"
    r"I can (do|fix|apply|implement|deploy|continue)|still needs? to|"
    r"remaining (work|job|steps?)|not (yet )?(fully )?(deployed|finished|complete))\b",
    re.IGNORECASE,
)
MAX_CONSECUTIVE_BLOCKS = 3


LOG_FILE = Path.home() / ".claude" / "logs" / "mimo_audit.log"

def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def redact_sensitive(text: str) -> str:
    text = re.sub(r'(?<![A-Za-z0-9])tp-[A-Za-z0-9]+', 'tp-<redacted>', text)
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


def compact(text: str, limit: int) -> str:
    text = redact_sensitive(text)
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[slice-elided by compactor - full content was present in the actual tool call]...\n" + text[-limit // 2 :]


# Set once per run so allow_stop can append the handoff report without threading it through every call site.
_HANDOFF: dict[str, str] = {}


def allow_stop(reason: str = "", *, unresolved: bool = False) -> None:
    log(f"ALLOW: {reason}")
    notice = _HANDOFF.get("text")
    if notice:
        # Reported, never enforced: a Stop hook that demanded a pushed commit could trap a lane with nothing to
        # commit or no network, which is the block loop this repo fixed on 2026-09-22.
        log(f"HANDOFF_CHECK: {'ready' if 'Backup verified' in notice else 'not ready'}")
        print(notice, file=sys.stderr)
    if unresolved:
        print(f"[{AUDITOR_NAME}] Review limit reached without PASS. "
              "Outstanding work is unresolved; this is not completion.", file=sys.stderr)
    sys.exit(0)


def block_stop(reason: str) -> None:
    log(f"BLOCK ({len(reason)} chars): {reason[:160].replace(chr(10), ' ')}...")
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.stdout.flush()
    sys.exit(0)


def block_audit_error(session_id: str, turn_id: str, iterations: int, message: str, cwd: str = "") -> None:
    """Reviewer could not run. Block, but only MAX_CONSECUTIVE_AUDIT_ERRORS Stops in a row:
    the counter is per session (per-turn iterations reset every Stop and never bound anything).
    On the cap, write an escalation record and release the turn as unresolved."""
    report_failure("mimo_audit", "audit_unavailable")
    save_iterations(session_id, turn_id, iterations + 1)
    save_verdict(session_id, turn_id, "ERROR", message)
    errors = increment_audit_errors(session_id)
    if errors >= MAX_CONSECUTIVE_AUDIT_ERRORS:
        path = write_escalation(session_id, cwd, message, errors)
        reset_audit_errors(session_id)
        log(f"AUDIT_ERROR_ESCALATED: session={session_id} errors={errors} file={path}")
        allow_stop(f"reviewer unavailable {errors}x in a row; escalation written to {path}", unresolved=True)
        return
    block_stop(
        f"[{AUDITOR_NAME} audit unavailable — attempt {errors}/{MAX_CONSECUTIVE_AUDIT_ERRORS}]\n\n"
        f"{message}\n\n"
        f"{AUDITOR_NAME} could not complete the audit. Keep working/retry; after "
        f"{MAX_CONSECUTIVE_AUDIT_ERRORS} consecutive failures the turn is released unresolved and an "
        f"escalation record is written."
    )


def filter_void_findings(verdict_text: str) -> tuple[str, list[str]]:
    """Drop void findings from an ISSUES verdict and renumber. Returns (text, dropped findings verbatim).
    The reviewer prompt already forbids these; medium effort still emits them (11 blocks on 2026-09-22,
    4 were this class). The filter makes the rule mechanical; every drop is logged in full."""
    numbered = re.compile(r"^\s*\d+[.)]\s+(\S.*)$")
    kept, dropped = [], []
    for line in verdict_text.splitlines()[1:]:
        m = numbered.match(line)
        if not m:
            kept.append(line)
            continue
        why = void_finding(m.group(1))
        if why:
            dropped.append(m.group(1))
            log(f"VOID_FINDING ({why}): {m.group(1)[:400]}")
        else:
            kept.append(m.group(1))
    if not dropped:
        return verdict_text, []
    findings = [ln for ln in kept if not numbered.match(ln) and ln.strip()]
    # `kept` now holds finding bodies (unnumbered) and any prose lines; renumber the bodies in order.
    out, n = [], 0
    for ln in kept:
        if ln.strip() and ln in findings:
            n += 1
            out.append(f"{n}. {ln}")
    if not n:
        return "VERDICT: PASS", dropped
    return "VERDICT: ISSUES\n" + "\n".join(out), dropped


def filter_unanchored_findings(verdict_text: str, slice_text: str) -> tuple[str, list[str]]:
    """Drop numbered findings whose [quote: "..."] anchor is not in the slice the reviewer saw.
    Premise-wrong findings ('no receipt shown' when the receipt is in the slice, 'claims complete' when it did
    not) were 14 of 30 lane rejections on 2026-09-22 at medium effort. A verbatim quote that must exist in the
    slice is the cheapest mechanical check on the reviewer's premise. Findings without any quote are kept and
    counted (NOQUOTE) so the rule can be tightened once the reviewer complies."""
    hay = _norm(slice_text)
    numbered = re.compile(r"^\s*\d+[.)]\s+(\S.*)$")
    kept, dropped, noquote = [], [], 0
    for line in verdict_text.splitlines()[1:]:
        m = numbered.match(line)
        if not m:
            continue
        body = m.group(1)
        q = QUOTE_RE.search(body)
        if not q:
            noquote += 1
            kept.append(body)
            continue
        if _norm(q.group(1)) in hay:
            kept.append(body)
        else:
            dropped.append(body)
            log(f"VOID_FINDING (unanchored quote): {body[:400]}")
    log(f"ANCHORS: kept={len(kept)} unanchored={len(dropped)} noquote={noquote}")
    if not dropped:
        return verdict_text, []
    if not kept:
        return "VERDICT: PASS", dropped
    return "VERDICT: ISSUES\n" + "\n".join(f"{i}. {b}" for i, b in enumerate(kept, 1)), dropped


def log_noise_classes(verdict_text: str) -> None:
    """Counter for the medium-vs-high comparison: findings that read as advisories or rest on elided output."""
    findings = [line for line in verdict_text.splitlines() if re.match(r"^\s*\d+[.)]\s+\S", line)]
    advisory = sum(1 for f in findings if ADVISORY_RE.search(f))
    elision = sum(1 for f in findings if ELISION_RE.search(f))
    if findings:
        log(f"NOISE_CLASS: findings={len(findings)} advisory={advisory} elision={elision}")


def has_tool_activity(transcript: str) -> bool:
    return any(marker in transcript for marker in TOOL_ACTIVITY_MARKERS)


def has_deliverable_claim(transcript: str) -> bool:
    return bool(DELIVERABLE_RE.search(transcript))


# A turn that edits nothing and asserts nothing is a relay, an ack or a hold ("Idle.", "Holding.",
# "Standing by.", forwarding a peer's status). Reviewing it costs a full model call and can only produce
# advisories. Measured 2026-09-22: 1,614 reviews, 133 with a code diff. The claim words below are the safety
# net - anything that asserts state (live, merged, green, a sha, a count, a PR number) is still reviewed.
STATUS_CLAIM_RE = re.compile(
    r"\b(live|trunk|deploy\w*|merg\w*|ship\w*|push\w*|committ?ed|fixed|implement\w*|complet\w*|done|"
    r"green|pass\w*|fail\w*|verified|applied|resolved|running|started|queued|open|ready|clean|"
    r"[0-9a-f]{7,40})\b"
    r"|#\d+|\d+\s*/\s*\d+|\d+\s*(tests?|checks?|rows?|files?|PRs?)\b",
    re.IGNORECASE)
# An offer or a plan is not a hold: a turn that stops while proposing work is exactly what the reviewer exists to catch.
HANDOFF_RE = re.compile(r"\b(I (can|could|will|would)\b|next:|next step|if you like|let me know|happy to|propose|suggest)", re.IGNORECASE)
CONTENTLESS_MAX_CHARS = 600


def contentless_turn(answer: str, edited: list) -> str:
    """Why the model call can be skipped ('' if it cannot). Edits, claims and status assertions all disqualify."""
    if edited:
        return ""
    text = (answer or "").strip()
    if not text or len(text) > CONTENTLESS_MAX_CHARS:
        return ""
    if STATUS_CLAIM_RE.search(text) or PENDING_WORK_RE.search(text) or HANDOFF_RE.search(text):
        return ""
    return f"no edits, no claim, {len(text)} chars"


def is_chat_turn(transcript: str) -> bool:
    # Review work requests regardless of how the assistant phrases its handoff.
    request, _, answer = transcript.rpartition("ASSISTANT:")
    return not (has_tool_activity(transcript) or has_deliverable_claim(transcript)
                or is_project_work(request) or PENDING_WORK_RE.search(answer))


def ask_mimo(transcript: str, max_tokens: int = 2500) -> str:
    prompt = AUDIT_PROMPT.format(transcript=transcript)
    content = call_mimo(
        prompt,
        max_tokens=max_tokens,
        timeout=AUDIT_TIMEOUT_S,
        lane="stop",
        system="Output only the required VERDICT format. Do not think aloud.",
    )
    if content:
        return normalize_verdict(content)
    log("EMPTY_RESPONSE")
    return ""


def ask_mimo_with_retry(transcript: str, max_tokens: int = 2500) -> str:
    for attempt in range(1, AUDIT_MAX_ATTEMPTS + 1):
        try:
            return ask_mimo(transcript, max_tokens=max_tokens)
        except NETWORK_RETRY_ERRORS as exc:
            if attempt >= AUDIT_MAX_ATTEMPTS:
                log(f"RETRY_NETWORK_EXHAUSTED: attempts={AUDIT_MAX_ATTEMPTS} error={exc}")
                raise RuntimeError(f"Codex audit transport failed after {AUDIT_MAX_ATTEMPTS} attempts: {exc}") from exc
            log(f"RETRY_NETWORK: attempt {attempt}/{AUDIT_MAX_ATTEMPTS} error={exc}")
        time.sleep(AUDIT_RETRY_DELAY_S)
    return ""


def repair_verdict_format(response: str) -> str:
    prompt = FORMAT_REPAIR_PROMPT.format(response=response[:2500])
    return normalize_verdict(call_mimo(
        prompt,
        max_tokens=500,
        timeout=AUDIT_TIMEOUT_S,
        lane="format_repair",
        system="You are a strict formatter. Output only the requested verdict format.",
    ))


# --- Main ------------------------------------------------------------------
def main() -> None:
    exit_if_audit_child()
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        allow_stop(f"bad payload: {e}")
        return

    why = is_rehome_turn(payload)
    if why:
        log(f"REHOME_FAST_EXIT: {why}")
        allow_stop("re-home turn: change_directory must apply within seconds of the turn ending")
        return

    # Normalize Claude payload → fills in synthesized turn_id and last_assistant_message.
    payload = normalize_payload(payload)
    session_id = payload.get("session_id") or "unknown"
    turn_id = retry_turn_id(session_id, payload, payload.get("turn_id") or "")
    transcript_path = payload.get("transcript_path") or ""
    last_msg = payload.get("last_assistant_message") or ""
    log(f"START session={session_id} turn={turn_id}")

    # The owner's off switch for the per-Stop reviewer (default on). CODEX_HOOK_CLAUDE_AUDIT only gates
    # stop_orchestrator's second pass, not this hook. A verdict is still written so the orchestrator does not
    # wait out its timeout; it is not PASS, so no auto-PR runs on an unreviewed turn.
    if hook_env("CLAUDE_STOP_REVIEW", "1").strip() == "0":
        save_verdict(session_id, turn_id, "SKIP_DISABLED", "CLAUDE_STOP_REVIEW=0")
        allow_stop("stop review disabled by CLAUDE_STOP_REVIEW=0")
        return

    try:
        _HANDOFF["text"] = handoff_notice(transcript_path, Path(payload.get("cwd") or "."), last_msg)
    except Exception as exc:   # never let the report break the gate
        log(f"HANDOFF_CHECK failed: {exc}")

    iterations = load_iterations(session_id, turn_id)
    if iterations >= MAX_ITERATIONS:
        pending_ids = [finding["id"] for finding in ledger.open_findings(session_id)]
        if pending_ids:
            log(f"LEDGER_RELEASED: {', '.join(pending_ids)} after max iterations; unresolved")
        ledger.clear(session_id)
        save_verdict(session_id, turn_id, "MAX_ALLOW", f"max iterations reached ({iterations}/{MAX_ITERATIONS})")
        allow_stop(f"max iterations reached ({iterations}/{MAX_ITERATIONS})", unresolved=True)
        return

    # Prefer Claude's transcript JSONL parser; fall back to last_assistant_message.
    transcript = latest_task_slice_claude(transcript_path, last_msg, MAX_TRANSCRIPT_CHARS) or redact_sensitive(last_msg)
    log(f"TASK_SLICE: {len(transcript)} chars")
    if len(transcript) < MIN_TRANSCRIPT_CHARS and is_chat_turn(transcript):
        save_verdict(session_id, turn_id, "SKIP_TOO_SHORT", f"transcript too short ({len(transcript)} chars)")
        reset_consecutive_blocks(session_id)
        allow_stop(f"transcript too short ({len(transcript)} chars)")
        return

    if is_chat_turn(transcript):
        save_verdict(session_id, turn_id, "SKIP_CHAT_TURN", "no edit tools, no deliverable claims")
        reset_consecutive_blocks(session_id)
        allow_stop("chat turn - audit skipped")
        return

    if not has_tool_activity(transcript):
        transcript = latest_task_slice_claude(transcript_path, transcript, MAX_TRANSCRIPT_CHARS, include_previous=True)

    # Deterministic pre-gates (no model call): unacknowledged check failure, leftover conflict
    # markers, unsupported live-response claims, and a peer-started merge/deploy with no owner
    # approval in this session (Dom, 2026-09-23: keep blocking; the lane answers [Fn] rejected).
    entries = latest_task_entries_claude(transcript_path)
    # The session cwd is often a non-git parent workspace; resolve to the repo the edits live in,
    # otherwise turn_diff silently no-ops (found: 12 edit turns, 0 diffs fed).
    repo_cwd = str(resolve_git_cwd(payload.get("cwd") or ".", [Path(p) for p in edited_paths(entries)], transcript))
    mechanical = deterministic_findings(entries, last_msg, repo_cwd)
    peer_gate = peer_triggered_unauthorized_action(entries, transcript_path, latest_turn_started_at(transcript_path))
    if peer_gate:
        mechanical.append(peer_gate)
    sensitive = sensitive_paths(entries)
    # Ledger: record the agent's [Fn] fixed/rejected lines; anything still open re-blocks with its own ID.
    unanswered = ledger.apply_decisions(session_id, last_msg)
    # The lesson was saved when the finding blocked; once the lane rejects it, other lanes must not learn it.
    for text in ledger.rejected_texts(session_id):
        forget_lesson(text)
    pre = [f"[{f['id']}] {f['text']}" for f in ledger.open_findings(session_id) if f["id"] in unanswered]
    if pre:
        log(f"LEDGER_UNANSWERED: {', '.join(unanswered)} — model call skipped")
    if mechanical:
        log(f"DETERMINISTIC_GATE: {len(mechanical)} finding(s) — model call skipped")
    if pre or mechanical:
        verdict_text = "VERDICT: ISSUES\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(pre + mechanical, 1))
    elif contentless_turn(last_msg, edited_paths(entries)):
        why = contentless_turn(last_msg, edited_paths(entries))
        log(f"SKIP_CONTENTLESS: {why} — model call skipped")
        save_verdict(session_id, turn_id, "SKIP_CONTENTLESS", why)
        reset_consecutive_blocks(session_id)
        allow_stop(f"contentless turn - audit skipped ({why})")
        return
    elif doc_only_turn(entries, last_msg, DELIVERABLE_RE):
        # Prose-only edits with no shipping claim: nothing a code reviewer should block on. $0.
        save_verdict(session_id, turn_id, "SKIP_DOC_ONLY", "doc-only edits, no deliverable claim")
        reset_consecutive_blocks(session_id)
        allow_stop("doc-only turn - audit skipped")
        return
    else:
        section = ledger.prompt_section(session_id)
        if section:
            transcript = f"{transcript}\n\n{section}"
        # The owner's own authorization-bearing messages in this session (never a peer's): the reviewer
        # must not call an action the owner delegated "unauthorized" because a peer supplied the timing.
        standing = owner_instructions(transcript_path, limit=3)
        if standing:
            log(f"OWNER_STANDING: {len(standing)} instruction(s)")
            transcript = f"{transcript}\n\nOWNER STANDING INSTRUCTIONS (the owner's words in this session, newest first):\n" + "\n".join(f"- [{ts}] {text}" for ts, text in standing)
        # What the answer cites from earlier turns (SHAs, PRs, times, pass counts) that the slice cut off.
        earlier = earlier_session_evidence(transcript_path, last_msg, transcript, latest_turn_started_at(transcript_path))
        if earlier:
            log(f"EARLIER_EVIDENCE: {len(earlier)} line(s)")
            transcript = f"{transcript}\n\nEARLIER IN THIS SESSION (tool output / peer messages before this turn, newest first):\n" + redact_sensitive("\n".join(f"- {row}" for row in earlier))
        if sensitive:
            log(f"SENSITIVE_PATHS: {', '.join(sensitive)}")
            transcript = f"{sensitive_section(sensitive)}\n\n{transcript}"
        since = latest_turn_started_at(transcript_path)
        committed = turn_commits_diff(repo_cwd, since)
        if committed:
            # What the turn committed, however it was edited, plus Edit-tool edits not yet committed.
            diff = "\n\n".join(x for x in (committed, turn_diff(repo_cwd, edited_paths(entries))) if x)[:DIFF_CAP + 2_000]
            log(f"TURN_COMMITS: {len(turn_commits(repo_cwd, since))} commit(s), {len(committed)} chars")
        else:
            diff = turn_diff(repo_cwd, edited_paths(entries), since=since)
        if diff:
            log(f"TURN_DIFF: {len(diff)} chars for {len(edited_paths(entries))} edited path(s)")
            transcript = (f"{transcript}\n\nWORKING TREE DIFF (the actual code change this turn — "
                          f"review the code itself, not only the agent's description of it):\n{diff}")
        try:
            verdict_text = ask_mimo_with_retry(transcript)
            if not verdict_text:
                retry_transcript = compact(transcript, 3_000)
                log(f"RETRY_EMPTY: {len(retry_transcript)} chars")
                verdict_text = ask_mimo_with_retry(retry_transcript, max_tokens=800)
        except Exception as e:
            block_audit_error(session_id, turn_id, iterations, f"audit API error: {e}", repo_cwd)
            return
        reset_audit_errors(session_id)  # reviewer answered; the outage streak is over

    log(f"{AUDITOR_NAME} verdict (iter {iterations + 1}): {verdict_text[:240]}")
    verdict_text = normalize_verdict(verdict_text)
    if sensitive and verdict_text.startswith("VERDICT: PASS"):
        warnings = pass_warnings(verdict_text)
        if warnings:
            # On auth/secrets/payment surfaces a warning is a finding. Upgrade deterministically.
            log(f"SENSITIVE_UPGRADE: {len(warnings)} warning(s) -> ISSUES on {', '.join(sensitive)}")
            verdict_text = "VERDICT: ISSUES\n" + "\n".join(f"{i}. {w}" for i, w in enumerate(warnings, 1))

    if verdict_text and not valid_verdict(verdict_text):
        try:
            repaired = repair_verdict_format(verdict_text)
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
                f"audit format error: {AUDITOR_NAME} returned no actionable verdict after repair",
            )
            return

    if verdict_text.startswith("VERDICT: PASS"):
        save_verdict(session_id, turn_id, "PASS", verdict_text)
        reset_consecutive_blocks(session_id)
        ledger.clear(session_id)
        allow_stop("audit passed")
        return

    if verdict_text.startswith("VERDICT: ISSUES"):
        log_noise_classes(verdict_text)
        verdict_text, unanchored = filter_unanchored_findings(verdict_text, transcript)
        verdict_text, dropped = filter_void_findings(verdict_text)
        dropped = unanchored + dropped
        if dropped and verdict_text.startswith("VERDICT: PASS"):
            log(f"VOID_ONLY: all {len(dropped)} finding(s) void; verdict -> PASS")
            save_verdict(session_id, turn_id, "PASS", verdict_text)
            reset_consecutive_blocks(session_id)
            ledger.clear(session_id)
            allow_stop("audit passed (only void findings)")
            return
    if verdict_text.startswith("VERDICT: ISSUES"):
        verdict_text, escalated = ledger.register_findings(session_id, verdict_text)
        if escalated:
            log(f"LEDGER_ESCALATED: {', '.join(escalated)} re-raised past MAX_REOPENS; dropped from block")
        if not any(ledger.NUMBERED_RE.match(line) for line in verdict_text.splitlines()):
            save_verdict(session_id, turn_id, "ESCALATED_ALLOW", verdict_text)
            reset_consecutive_blocks(session_id)
            allow_stop(f"all findings escalated ({', '.join(escalated)}); released unresolved", unresolved=True)
            return
        save_iterations(session_id, turn_id, iterations + 1)
        remember_blocked_turn(session_id, turn_id)
        save_verdict(session_id, turn_id, "ISSUES", verdict_text)
        consecutive_blocks = increment_consecutive_blocks(session_id)
        if consecutive_blocks == 1:
            # Only the first block in a chain is a review of the work. Later rounds
            # re-litigate a rebuttal and would pollute Active Lessons for every future turn.
            record_lesson("mimo_stop", verdict_text.splitlines()[1] if len(verdict_text.splitlines()) > 1 else verdict_text, session_id,
                          lesson_lane(transcript_path, payload.get("cwd") or "", session_id))
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            log(f"BLOCK_CHAIN_DAMPER: session={session_id} blocks={consecutive_blocks}")
            save_verdict(session_id, turn_id, "DAMPER_ALLOW", verdict_text)
            reset_consecutive_blocks(session_id)
            ledger.clear(session_id)  # the damper is an escalation; do not re-block on these next Stop
            allow_stop(f"block chain damped after {consecutive_blocks} consecutive blocked turns", unresolved=True)
            return
        feedback = (
            f"[{AUDITOR_NAME} code-review hook — iteration "
            f"{iterations + 1}/{MAX_ITERATIONS}]\n\n"
            f"A fresh {AUDITOR_NAME} session reviewed your work and found issues. "
            f"Fix these before stopping:\n\n"
            f"{verdict_text}\n\n"
            f"{ledger.DECISION_INSTRUCTIONS}\n\n"
            f"After addressing each item, run any relevant checks and continue."
        )
        block_stop(feedback)
        return

    save_iterations(session_id, turn_id, iterations + 1)
    save_verdict(session_id, turn_id, "UNPARSEABLE", verdict_text)
    feedback = (
        f"[{AUDITOR_NAME} code-review hook — iteration "
        f"{iterations + 1}/{MAX_ITERATIONS}]\n\n"
        f"{AUDITOR_NAME} did not return a parseable PASS/ISSUES verdict. Treat this as "
        f"an audit failure, re-check the user's request, verify the work with "
        f"strong relevant tests/logs, and only stop once the audit passes.\n\n"
        f"Raw verdict preview: {verdict_text[:120]!r}"
    )
    block_stop(feedback)


if __name__ == "__main__":
    main()

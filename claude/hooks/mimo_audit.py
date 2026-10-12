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
import shlex
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import forget_lesson, hook_env, lesson_lane, record_lesson  # noqa: E402
from lib.api import AUDITOR_NAME, call_mimo, exit_if_audit_child  # noqa: E402
from lib.policies import is_project_work  # noqa: E402
from lib.audit_state import (  # noqa: E402
    increment_audit_errors, increment_consecutive_blocks, load_iterations, remember_blocked_turn, reset_audit_errors,
    reset_consecutive_blocks, retry_turn_id, save_iterations, save_verdict, write_escalation,
)
from lib.audit_prompt import AUDIT_PROMPT, FORMAT_REPAIR_PROMPT  # noqa: E402
from lib.verdicts import ADVISORY_RE, ELISION_RE, QUOTE_RE, _norm, normalize_verdict, valid_verdict, verdict_status, void_finding  # noqa: E402
from lib.transcript import DIFF_CAP, deterministic_findings, doc_only_turn, edited_paths, pass_warnings, sensitive_paths, sensitive_section, turn_commits, turn_commits_diff, turn_diff  # noqa: E402
from lib import ledger  # noqa: E402
from lib.git import resolve_git_cwd, trunk_branch  # noqa: E402
from lib.rehome import is_rehome_turn  # noqa: E402
from lib.handoff_check import handoff_notice  # noqa: E402
from lib.claude_adapter import normalize_payload, latest_task_slice_claude, latest_task_entries_claude, latest_turn_started_at, owner_instructions, peer_triggered_unauthorized_action, earlier_session_evidence  # noqa: E402


# --- Tunables --------------------------------------------------------------
MAX_ITERATIONS = 2   # 2026-10-08 (Dom, Optimizer item 5a): was 3; 111 of 294 blocks in 24 h ended at the cap, fixing nothing
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
        message = (f"[{AUDITOR_NAME}] Review limit reached without PASS. "
                   "Outstanding work is unresolved; this is not completion.")
        print(message, file=sys.stderr)
        # Claude Code shows a successful hook's stderr only in the debug log; systemMessage is the visible channel.
        sys.stdout.write(json.dumps({"systemMessage": message}))
        sys.stdout.flush()
    sys.exit(0)


def block_stop(reason: str) -> None:
    log(f"BLOCK ({len(reason)} chars): {reason[:160].replace(chr(10), ' ')}...")
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.stdout.flush()
    sys.exit(0)


# Errors a retry cannot fix: the reviewer's account is out of quota or logged out. Blocking on them only costs the
# lane turns (2026-09-30: the weekly limit hit at 18:43 and every lane lost two turns per Stop, 63 failures in 11 min,
# with the reset five days away). Timeouts and network errors still block and retry, as before.
ACCOUNT_ERROR_RE = re.compile(
    r"hit your (?:weekly|daily|monthly|session|usage) limit|usage limit reached|"
    r"OAuth session expired|Failed to authenticate|not logged in|Invalid API key",
    re.IGNORECASE,
)


def block_audit_error(session_id: str, turn_id: str, iterations: int, message: str, cwd: str = "") -> None:
    """Reviewer could not run. Block, but only MAX_CONSECUTIVE_AUDIT_ERRORS Stops in a row:
    the counter is per session (per-turn iterations reset every Stop and never bound anything).
    On the cap, write an escalation record and release the turn as unresolved.
    An account error (quota, login) releases at once: retrying cannot fix it."""
    save_iterations(session_id, turn_id, iterations + 1)
    save_verdict(session_id, turn_id, "ERROR", message)
    if ACCOUNT_ERROR_RE.search(message or ""):
        reset_audit_errors(session_id)
        log(f"AUDIT_ACCOUNT_DOWN: {message[:200]}")
        allow_stop("reviewer account unavailable (quota or login); turn released unreviewed", unresolved=True)
        return
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


# Dom, 2026-10-09 (via Strategy): "over-zealous and not material... code catches and quality bugs important but not just
# text words." On a turn with no diff, a finding blocks only when the quoted claim is about the state of the world:
# live/merged/deployed/installed/passed/fixed, PRs, CI, commits, destructive actions. Wording (delivered vs queued,
# starting vs asked to start, "aligned", tense, attribution) is logged and never blocks. A finding with no quote is kept.
# One list of state-of-the-world verbs for BOTH the wording filter and the no-tool skip (Auditor, #117): a claim that
# something crashed, is held, was reinstalled, rolled back, landed or is down is never "wording".
STATE_VERBS = (r"crash\w*|held|on hold|reinstall\w*|roll(?:ed|ing)?[\s-]*back|landed|down|stopped|broke\w*|"
               r"restor\w*|paused|blocked|stuck|cancel\w*|disabled|enabled|unmerged|restart\w*|revert\w*|reject\w*|"
               r"running|published|done|complete\w*|trunk|exists?|offline|launched")
NUMBER_WORDS = r"(?:(?<!no )(?<!any )(?<!every )(?<!some )one(?!\s+(?:of|else|another|who)\b))|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|dozen"   # a count is a claim, but not a skip trigger
MATERIAL_CLAIM_RE = re.compile(
    r"\b(" + STATE_VERBS + r"|" + NUMBER_WORDS + r"|live|merged?|deployed?|installed?|pass(?:ed|es)?|green|fixed|pushed|shipped|released|deleted|removed|wiped|"
    r"reverted|killed|mergeable|PRs?|CI|checks?|commits?|open|gh|saved|written|wrote|created|updated|ran|"
    r"fail\w*|error\w*|assert\w*|exception|traceback|timed?\s*out)\b|#\d+|\b[0-9a-f]{7,40}\b"
    r"|[=;{}]|=>|\(\)", re.IGNORECASE)   # failure output and code punctuation are never wording


# QUOTE_RE stops at an apostrophe ("I've told Lead"), which would leave such a finding unquoted and so kept.
CLAIM_QUOTE_RE = re.compile(r"\[quote:\s*[\"“](.{3,400}?)[\"”]\s*\]", re.IGNORECASE)


# 2026-10-12 replay (65 downgraded findings): the #117 extension of wording-advisory to a turn WITH a diff downgraded a real
# premature stop ("Want me to do both Monday?"). Wording is advisory only on a turn with no diff; a diff turn always blocks.
# 2026-10-12 replay of 65 downgraded findings (fixture tests/wording_replay_2026_10_12.json): 39 were real defects (a hidden
# failure, a premature stop, an unbacked or false claim, a bad command), 22 only restated a peer's message or an order as done.
# A keyword list of defect words failed the Auditor's probes (any defect worded without the keywords came back advisory), so this
# is an ALLOWLIST of the whole body (Auditor, #127): on a no-diff turn a finding is advisory only when its quote carries no state word
# AND, after removing the quote and the "Replace with / Say instead" advice, EVERY sentence has a known attribution shape (an order or
# message is the sole support, a peer has not confirmed, a paraphrase of a peer is off). Anything left over blocks. Strategy's rule
# stays: a state word, number or id in the quote keeps blocking.
_PEER = r"(?:Deploy|Web|Backend|Lead|Strategy|the COO|COO|the Auditor|Auditor|World|Combat|Characters|Hooks Dev|Duels|another lane|the peer|the agent|a peer)"
# Advice to the lane ("Replace it with: \"...\""): only the cue and its own quoted replacement are removed, never the rest of the body
# (Auditor, #127: a defect sentence after the advice used to be stripped with it). A cue with no quoted replacement stays, so it has
# to match an attribution shape itself. The gap before the quote may not cross a sentence end.
_SUGGESTION_RE = re.compile(
    r"\b(?:Replace (?:it|the sentence|this|with)|Say instead|Write|Change (?:it|the line|this)|Relabel|Either|Fix:|Check the)\b"
    r"(?:[^\"\u201c.!?;]|\.(?=\S)){0,200}[\"\u201c][^\"\u201d]*[\"\u201d]\.?", re.S)
ATTRIBUTION_SENTENCES = [re.compile(p,re.I) for p in (
 r"^Nothing in (?:the|this) slice backs this\.?$",
 r"^This (?:states|says|is stated)[^.]{0,80}(?:as fact|is done|stated as fact)[^.]{0,60}$",
 r"^This states [^.]{0,60} as fact, but nothing in the slice confirms it$",
 r"^This (?:states|says)[^.]{0,60}but the (?:slice|only support) [^.]{0,120}$",
 r"^(?:The )?only (?:support|evidence)(?: for this)? is [^.;]{0,140}$",
 r"^(?:Its|The|That) (?:send )?(?:result|delivery receipt|message) [^.;]{0,60}(?:not (?:yet )?(?:read|confirmed)|was \"not yet read)[^.;]{0,60}$",
 r"^(?:Your )?messages? to [^.;]{0,80}(?:not (?:yet )?(?:read|confirmed)|reported as not confirmed read)[^.;]{0,40}$",
 r"^SendMessage returned \"not[^\"]*\"$",
 r"^Nothing (?:confirms|in (?:the|this) slice (?:shows|confirms|backs)) "+_PEER+r"[^.;]{0,160}$",
 r"^Nothing confirms "+_PEER+r"[^.;]{0,100}$",
 r"^Nothing in (?:the|this) slice shows "+_PEER+r"[^.;]{0,160}$",
 r"^Nothing in (?:the|this) slice shows "+_PEER+r" (?:working|building|started)[^.;]{0,100}$",
 r"^Nothing in the slice shows the COO replied or acted, so this states an order you sent as work that is under way$",
 r"^Nothing in the slice backs this: no reply from "+_PEER+r"[^.;]{0,160}$",
 r"^No (?:reply|tool output|tool call)[^.;]{0,120}(?:confirm|shows|reads replies|appears in the slice)[^.;]{0,120}$",
 r"^No reply from "+_PEER+r"[^.;]{0,60}$",
 r"^(?:Its|The) result says [^.;]{0,40}\"?not confirmed read\"?[^.;]{0,50}$",
 r"^The only visible messages are the orders that were sent$",
 r"^Neither "+_PEER+r" nor "+_PEER+r" has confirmed[^.;]{0,80}$",
 r"^"+_PEER+r" hasn't started yet[^.;]{0,60}$",
 r"^This (?:sentence )?says the work is (?:done|under way)[^.;]{0,160}$",
 r"^Hooks Dev said [^.;]{0,120}$|^It did not say [^.;]{0,80}$",
 r"^The (?:agent's )?(?:report|message) says [^.;]{0,160}, not [^.;]{0,60}$",
 r"^The Lead's message says [^.;]{0,100}$|^Lead's message names this [^.;]{0,100}, not item \d+$",
 r"^Calling it [^.;]{0,60} gives it a meaning nobody stated[^.;]{0,120}$",
 r"^Unless [^.;]{0,100}has been confirmed[^.;]{0,100}$",
 r"^Its result says [^.;]{0,50}and no reply came back$",
 r"^the PR (?:list|#\d+ output) (?:came back empty|shows only [^.;]{0,60})$",
 r"^no reply came back$",
 r"^,?\s*or show a reply or PR from [^.;]{0,60}$|^or say where the new number came from$|^Relabel it as [^.;]{0,100}$",
)]


def _body_sentences(body: str) -> list[str]:
    text = re.sub(r"^\s*\d+[.)]\s*", "", body)
    text = re.sub(r"^\s*\[F\d+\]\s*", "", text)
    text = _SUGGESTION_RE.sub("", re.sub(r"\[quote:[^\]]*\]", "", text))
    return [x.strip().rstrip(".") for x in re.split(r"(?<=[.!?])\s+|;\s+|,\s+and\s+(?=no |the )", text) if x.strip().rstrip(".")]


def is_wording_finding(body: str, no_diff: bool) -> bool:
    q = CLAIM_QUOTE_RE.search(body)
    if not (no_diff and q) or MATERIAL_CLAIM_RE.search(q.group(1)):
        return False
    sentences = _body_sentences(body)
    return bool(sentences) and all(any(p.match(x) for p in ATTRIBUTION_SENTENCES) for x in sentences)


def filter_wording_findings(verdict_text: str, no_diff: bool) -> tuple[str, list[str]]:
    """Drop findings whose quoted claim carries no state-of-the-world word. Returns (text, dropped); the caller shows
    the dropped ones as an advisory. On a turn with a diff, numbers, ids and code references still block."""
    numbered = re.compile(r"^\s*\d+[.)]\s+(\S.*)$")
    kept, dropped = [], []
    for line in verdict_text.splitlines()[1:]:
        m = numbered.match(line)
        if not m:
            continue
        body = m.group(1)
        if is_wording_finding(body, no_diff):
            dropped.append(body)
            log(f"WORDING_FINDING (not material, {'no diff' if no_diff else 'diff turn'}): {body[:400]}")
        else:
            kept.append(body)
    if not dropped:
        return verdict_text, []
    if not kept:
        return "VERDICT: PASS", dropped
    return "VERDICT: ISSUES\n" + "\n".join(f"{i}. {b}" for i, b in enumerate(kept, 1)), dropped


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
    r"\b(" + STATE_VERBS + r"|live|trunk|deploy\w*|merg\w*|ship\w*|push\w*|committ?ed|fixed|implement\w*|complet\w*|done|"
    r"green|pass\w*|fail\w*|verified|applied|resolved|running|started|queued|open|ready|clean|"
    r"[0-9a-f]{7,40})\b"
    r"|#\d+|\d+\s*/\s*\d+|\d+\s*(tests?|checks?|rows?|files?|PRs?)\b",
    re.IGNORECASE)
# An offer or a plan is not a hold: a turn that stops while proposing work is exactly what the reviewer exists to catch.
HANDOFF_RE = re.compile(r"\b(I (can|could|will|would)\b|next:|next step|if you like|let me know|happy to|propose|suggest)", re.IGNORECASE)
CONTENTLESS_MAX_CHARS = 600


RELAY_TOOLS = ("SendMessage", "ReadNotifications", "ListAgents", "Read")   # Monitor runs a shell command: not a relay
RELAY_MAX_CHARS = 2000


def relay_only_turn(entries: list) -> bool:
    """True when every tool call this turn was a send or a read (2026-10-08, Optimizer item 5c)."""
    calls = [body for role, body in entries if role == "TOOL CALL"]
    return bool(calls) and all(body.startswith(RELAY_TOOLS) for body in calls)


READ_ONLY_TOOLS = frozenset({
    "Read", "Grep", "Glob", "ToolSearch", "ReadNotifications", "ListAgents", "TaskOutput", "BashOutput",
    "mcp__semble__search", "mcp__semble__find_related",
    *(f"mcp__Claude_Browser__{n}" for n in ("read_page", "get_page_text", "find", "read_console_messages",
                                            "read_network_requests", "tabs_context"))})
_RO_SIMPLE = frozenset("cat head tail wc ls stat du df pwd echo printf date which uptime jq sort uniq cut tr diff cmp "
                       "sha256sum shasum".split())
_RO_GIT = frozenset("log show diff status rev-parse ls-files ls-tree cat-file merge-base blame describe fetch show-ref "
                    "for-each-ref".split())
_RO_REDIRECT_RE = re.compile(r"(\d?>&\d|\d?>\s*/dev/null|&>\s*/dev/null)")   # the only redirects a read may carry
_RO_DENY_RE = re.compile(r"[;`<>&\n]|\$\(|\|\||\b(tee|xargs|ssh|scp|rsync|sudo|eval)\b|\b(bash|sh|zsh)\s+-c\b|\b(python3?|node)\s+-[ce]\b")
_RO_TMP = ("/tmp/", "/private/tmp/")
# Flags that write, delete or run another program, however the value is attached (-XPOST, --pre=cmd, -fquery=...).
_RO_BAD_FLAG_RE = {
    "git": re.compile(r"^(-c|--config-env|--upload-pack|--receive-pack|--exec|--ext-diff|--textconv|--output|--open-files-in-pager|--paginate|--exec-path)"),
    "rg": re.compile(r"^(--pre|--hostname-bin)"),
    "sort": re.compile(r"^(-o|--output|--compress-program|-T|--temporary-directory)"),
    "gh": re.compile(r"^(-X|-f|-F|--method|--field|--raw-field|--input|--hostname)"),
}


_CURL_FLAGS = frozenset("--silent --show-error --location --fail --head --include --verbose --compressed --insecure".split())
_CURL_VALUE_FLAGS = frozenset("-H --header -m --max-time --connect-timeout --retry -A --user-agent".split())


def _read_only_curl(args: list[str]) -> bool:
    """A flag ALLOW-list (Auditor): any curl option not named here may write a file or send a command, so it fails closed."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-o", "--output") or (arg.startswith("-o") and not arg.startswith("--") and not re.fullmatch(r"-[sSLfIivk]+", arg)):
            target = arg[2:] if arg.startswith("-o") and len(arg) > 2 else (args[i + 1] if i + 1 < len(args) else "")
            if not target.startswith(_RO_TMP) or ".." in target.split("/"):
                return False
            i += 1 if arg.startswith("-o") and len(arg) > 2 else 2
        elif arg in _CURL_VALUE_FLAGS:
            i += 2
        elif arg in _CURL_FLAGS or re.fullmatch(r"-[sSLfIivk]+", arg):
            i += 1
        elif arg.startswith("-"):
            return False
        elif re.match(r"https?://", arg):
            i += 1   # gopher://, dict://, telnet:// and bare hosts can send raw commands to local services
        else:
            return False
    return True


def _read_only_segment(tokens: list[str]) -> bool:
    cmd, args = tokens[0], tokens[1:]
    if re.match(r"\w+=", cmd):
        return False   # an env VAR= prefix can change what a command does
    flags = " ".join(args)
    bad = _RO_BAD_FLAG_RE.get(cmd)
    if cmd in _RO_SIMPLE:
        if cmd == "date" and re.search(r"(^|\s)(-s|--set)", flags):
            return False
        if cmd == "sort" and any(bad.match(a) for a in args):
            return False
        return not (cmd == "uniq" and len([a for a in args if not a.startswith("-")]) > 1)   # uniq IN OUT writes OUT
    if cmd == "rg":
        return not any(bad.match(a) for a in args)
    if cmd == "grep":
        return True
    if cmd == "command":
        return args[:1] == ["-v"]
    if cmd == "sed":
        return len(args) >= 2 and args[0] == "-n" and bool(re.fullmatch(r"['\"]?[0-9$,]*(/[^/]*/)?p['\"]?", args[1]))
    if cmd == "find":
        return not re.search(r"(^|\s)-(delete|exec|execdir|ok|okdir|fprint\w*|fls)\b", flags)
    if cmd == "git":
        rest = list(args)
        if not rest or any(bad.match(a) for a in rest) or rest[0] == "-C":   # -C <dir> may name a planted repo whose config runs code
            return False
        if rest[0] == "fetch" and any(":" in a for a in rest[1:]):   # a refspec writes a local ref
            return False
        if rest[0] in _RO_GIT:
            return True
        return (rest[0] == "branch" and rest[1:2] in (["--list"], ["-a"])) or rest in (["remote", "-v"], ["worktree", "list"], ["stash", "list"])   # nothing after: `remote -v add` really adds
    if cmd == "gh":
        if any(bad.match(a) for a in args) or "graphql" in args:
            return False
        if args[:1] in (["pr"], ["issue"]):
            return args[1:2] in (["view"], ["diff"], ["checks"], ["list"], ["status"])
        if args[:1] == ["run"]:
            return args[1:2] in (["list"], ["view"])
        return args[:1] == ["api"]
    if cmd == "curl":
        return _read_only_curl(args)
    return False


def read_only_bash(command: str) -> bool:
    """Auditor's fail-closed allow-list (2026-10-10): every pipe segment must be a plain read. Anything unparsed is NOT read-only."""
    text = _RO_REDIRECT_RE.sub(" ", command or "").strip()
    if not text or _RO_DENY_RE.search(text):
        return False
    try:
        segments = [shlex.split(seg) for seg in text.split("|")]
    except ValueError:
        return False
    return all(tokens and _read_only_segment(tokens) for tokens in segments)


def read_only_turn(entries: list) -> bool:
    """True when every tool call this turn is a read; a send, an edit, a push or any unknown tool makes it False (review it)."""
    for role, body in entries:
        if role != "TOOL CALL":
            continue
        name = body.split(":", 1)[0].strip()
        if name == "Bash":
            try:
                command = json.loads(body.split("] ", 1)[1]).get("command", "")
            except (IndexError, ValueError, AttributeError):
                return False
            if not read_only_bash(command):
                return False
        elif name not in READ_ONLY_TOOLS:
            return False
    return True


def contentless_turn(answer: str, edited: list, relay_only: bool = False) -> str:
    """Why the model call can be skipped ('' if it cannot). Edits, claims and status assertions all disqualify."""
    if edited:
        return ""
    text = (answer or "").strip()
    if not text or len(text) > (RELAY_MAX_CHARS if relay_only else CONTENTLESS_MAX_CHARS):
        return ""
    if STATUS_CLAIM_RE.search(text) or PENDING_WORK_RE.search(text) or HANDOFF_RE.search(text):
        return ""
    return f"no edits, no claim, {len(text)} chars"


def no_tool_calls(entries: list, last_msg: str, transcript_path: str = "") -> str:
    """Why the model call can be skipped for a turn with no tool call at all ('' if it cannot). Dom's ruling via Strategy,
    2026-10-10: a turn with no tool calls and no diff cannot hide a failed check or an unauthorized action, the two things the
    reviewer exists to catch, unless the text claims a run, a check, a deploy, a pass count or a live/merged state (a fabricated
    receipt needs no tool call). The caller still reviews a turn that left a diff or commits, and the deterministic gate runs first."""
    text = (last_msg or "").strip()
    if any(role == "TOOL CALL" for role, _ in entries) or not text or not Path(transcript_path or "/nonexistent").is_file():
        return ""   # an unreadable transcript means "unknown", not "no tool calls": review it
    if PENDING_WORK_RE.search(text) or HANDOFF_RE.search(text):
        return ""   # stopping on an offer or unfinished work is premature stopping, the third thing the reviewer exists to catch
    if STATUS_CLAIM_RE.search(text):
        return ""   # Strategy, option B: a run / verification / deploy / pass-count / live / merged claim is a possible fabricated receipt
    return f"no tool calls, {len(text)} chars"


def worktree_snapshot(repo_cwd: str, since: str = "", cap: int = DIFF_CAP) -> str:
    """Everything now different from HEAD ('' when clean, not a repo, or git fails): untracked files and directories
    that appeared since the turn began (by inode change time, so `cp -p`, `tar x` and `curl -R`, which keep an old
    mtime, still count), then the tracked diff. The evidence for a turn that changed code through Bash and so has no
    Edit-tool paths. A path untracked before the turn is not this turn's change. Every changed path counts for the
    skip decision; only the evidence is capped, and it says so. Each part stands alone: a dangling symlink or an
    unreadable file costs only itself, never the tracked diff. Known limit: an edit inside a nested repository that
    existed before the turn is invisible here (git lists only the directory and its change time does not move)."""
    import subprocess
    from datetime import datetime
    tracked = ""
    try:
        out = subprocess.run(["git", "-C", repo_cwd, "diff", "HEAD", "--no-color"], capture_output=True, text=True, timeout=10)
        tracked = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        start = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() if since else None
        names = subprocess.run(["git", "-C", repo_cwd, "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True, timeout=5).stdout.splitlines()
    except (OSError, ValueError, subprocess.SubprocessError):
        start, names = None, []
    new_paths: list[str] = []
    for name in names[:20_000]:
        try:
            st = (Path(repo_cwd) / name).stat()
            if start is None or max(st.st_mtime, st.st_ctime) >= start:
                new_paths.append(name)
        except OSError:
            continue   # a dangling link or a path that vanished
    shown: list[str] = []
    for name in new_paths[:5]:
        file = Path(repo_cwd) / name
        try:
            if file.is_file():
                with open(file, errors="replace") as handle:
                    shown.append(f"--- NEW FILE {name} ---\n{handle.read(800)}")
            else:   # a nested repository or directory: name it, its content is not ours to read here
                shown.append(f"--- NEW DIRECTORY {name} (untracked content inside) ---")
        except OSError:
            shown.append(f"--- NEW PATH {name} (unreadable) ---")
    notes = []
    if len(new_paths) > 5:
        notes.append(f"...[{len(new_paths) - 5} more new paths not shown]")
    room = max(cap - sum(len(x) for x in shown), cap // 2)
    if len(tracked) > room:
        notes.append(f"...[tracked diff truncated: {len(tracked) - room} more chars]")
    parts = [*shown, *([tracked[:room]] if tracked else []), *notes]
    return "\n\n".join(parts)


def is_chat_turn(transcript: str) -> bool:
    # Review work requests regardless of how the assistant phrases its handoff.
    request, _, answer = transcript.rpartition("ASSISTANT:")
    return not (has_tool_activity(transcript) or has_deliverable_claim(transcript)
                or is_project_work(request) or PENDING_WORK_RE.search(answer))


class AuditBudgetExceeded(RuntimeError):
    """The whole-attempt deadline passed; not retried."""


# One absolute deadline for the whole attempt (every model call, retry, repair and the final output), set in main().
# 2026-10-07 audit: retry + empty-response + format-repair could add up to 501 s against the 240 s hook budget.
AUDIT_ATTEMPT_BUDGET_S = 205      # < AUDIT_HOOK_TIMEOUT_S (240)
AUDIT_FINISH_RESERVE_S = 5        # kept back for cleanup and the final stdout write
AUDIT_MIN_CALL_S = 5
_DEADLINE: list[float] = []


def call_timeout() -> float:
    """Timeout for the next model call: the per-call cap, clipped to what is left of the attempt."""
    if not _DEADLINE:
        return AUDIT_TIMEOUT_S
    left = _DEADLINE[0] - time.monotonic() - AUDIT_FINISH_RESERVE_S
    if left < AUDIT_MIN_CALL_S:
        raise AuditBudgetExceeded(f"audit budget of {AUDIT_ATTEMPT_BUDGET_S}s exhausted")
    return min(AUDIT_TIMEOUT_S, left)


def ask_mimo(transcript: str, max_tokens: int = 2500) -> str:
    prompt = AUDIT_PROMPT.format(transcript=transcript)
    content = call_mimo(
        prompt,
        max_tokens=max_tokens,
        timeout=call_timeout(),
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
        time.sleep(min(AUDIT_RETRY_DELAY_S, max(0.0, _DEADLINE[0] - time.monotonic())) if _DEADLINE else AUDIT_RETRY_DELAY_S)
    return ""


def repair_verdict_format(response: str) -> str:
    prompt = FORMAT_REPAIR_PROMPT.format(response=response[:2500])
    return normalize_verdict(call_mimo(
        prompt,
        max_tokens=500,
        timeout=call_timeout(),
        lane="format_repair",
        system="You are a strict formatter. Output only the requested verdict format.",
    ))


# --- Main ------------------------------------------------------------------
def main() -> None:
    _DEADLINE[:] = [time.monotonic() + AUDIT_ATTEMPT_BUDGET_S]
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

    # The owner's off switch for the per-Stop reviewer (default on). A verdict is still written so audit_state's
    # retry and iteration bookkeeping sees this turn; it is not PASS.
    if hook_env("CLAUDE_STOP_REVIEW", "1").strip() == "0":
        log("SKIP_DISABLED: CLAUDE_STOP_REVIEW=0 — model call skipped")
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
        log(f"MAX_ALLOW: {iterations}/{MAX_ITERATIONS} iterations — model call skipped")
        save_verdict(session_id, turn_id, "MAX_ALLOW", f"max iterations reached ({iterations}/{MAX_ITERATIONS})")
        allow_stop(f"max iterations reached ({iterations}/{MAX_ITERATIONS})", unresolved=True)
        return

    # Prefer Claude's transcript JSONL parser; fall back to last_assistant_message.
    transcript = latest_task_slice_claude(transcript_path, last_msg, MAX_TRANSCRIPT_CHARS) or redact_sensitive(last_msg)
    log(f"TASK_SLICE: {len(transcript)} chars")
    if len(transcript) < MIN_TRANSCRIPT_CHARS and is_chat_turn(transcript):
        log(f"SKIP_TOO_SHORT: {len(transcript)} chars — model call skipped")
        save_verdict(session_id, turn_id, "SKIP_TOO_SHORT", f"transcript too short ({len(transcript)} chars)")
        reset_consecutive_blocks(session_id)
        allow_stop(f"transcript too short ({len(transcript)} chars)")
        return

    if is_chat_turn(transcript):
        log("SKIP_CHAT_TURN: no edit tools, no deliverable claims — model call skipped")
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
    # One snapshot of the turn's change, used for the skip decision AND the evidence: the turn's commits plus everything
    # now different from HEAD plus untracked files created since the turn began, Edit-tool turns included.
    since = latest_turn_started_at(transcript_path)
    snapshot = worktree_snapshot(repo_cwd, since) if any(role == "TOOL CALL" for role, _ in entries) else ""
    # Ledger: record the agent's [Fn] fixed/rejected lines; anything still open re-blocks with its own ID.
    unanswered = ledger.apply_decisions(session_id, last_msg)
    # The lesson was saved when the finding blocked; once the lane rejects it, other lanes must not learn it.
    for text in ledger.rejected_texts(session_id):
        forget_lesson(text)
    pre = [f"[{f['id']}] {f['text']}" for f in ledger.open_findings(session_id) if f["id"] in unanswered]
    released = False
    if pre:   # 2026-10-08 (Dom, Optimizer item 5b): a missing "[Fn] fixed:" line is noted once, never a BLOCK
        log(f"LEDGER_UNANSWERED: {', '.join(unanswered)} — noted, not blocked")
        _HANDOFF["text"] = (f"{_HANDOFF.get('text') or ''}\nUnanswered review finding(s) {', '.join(unanswered)} released: "
                            "the final message had no [Fn] fixed:/rejected: line.").strip()
        ledger.release(session_id, unanswered)
        pre, released = [], True
    if mechanical:
        log(f"DETERMINISTIC_GATE: {len(mechanical)} finding(s) — model call skipped")
    if pre or mechanical:
        verdict_text = "VERDICT: ISSUES\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(pre + mechanical, 1))
    elif not released and (relay_only_turn(entries) or read_only_turn(entries)) and contentless_turn(last_msg, edited_paths(entries), relay_only_turn(entries)) and not (turn_commits(repo_cwd, since) or snapshot):
        why = contentless_turn(last_msg, edited_paths(entries), relay_only_turn(entries))
        log(f"SKIP_CONTENTLESS: {why} — model call skipped")
        save_verdict(session_id, turn_id, "SKIP_CONTENTLESS", why)
        reset_consecutive_blocks(session_id)
        allow_stop(f"contentless turn - audit skipped ({why})")
        return
    elif not released and no_tool_calls(entries, last_msg, transcript_path) and not (turn_commits(repo_cwd, since) or snapshot):
        why = no_tool_calls(entries, last_msg, transcript_path)
        log(f"SKIP_NO_TOOLS: {why} — model call skipped")
        log(f"SKIP_NO_TOOLS_TEXT: {redact_sensitive(last_msg)[:600]!r}")   # so the 24 h replay can audit the skip rule itself
        save_verdict(session_id, turn_id, "SKIP_NO_TOOLS", why)
        reset_consecutive_blocks(session_id)
        allow_stop(f"no-tool turn - audit skipped ({why})")
        return
    elif not released and doc_only_turn(entries, last_msg, DELIVERABLE_RE):   # a released stop always gets one review
        # Prose-only edits with no shipping claim: nothing a code reviewer should block on. $0.
        log("SKIP_DOC_ONLY: doc-only edits, no deliverable claim — model call skipped")
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
        # 2026-09-28: Frankendom's trunk is codex/01a09a76/task-1; the reviewer flagged "targets trunk" as false 3x in one
        # night because a PR's base "is not main". Name the trunk when it is not one the reviewer would guess.
        trunk = trunk_branch(repo_cwd)
        if trunk and trunk not in ("main", "master"):
            transcript = f"{transcript}\n\nREPOSITORY TRUNK: {trunk} (origin/HEAD). 'trunk' and 'main' in the lane's words mean this branch; a PR based on it targets trunk."
        # What the answer cites from earlier turns (SHAs, PRs, times, pass counts) that the slice cut off.
        earlier = earlier_session_evidence(transcript_path, last_msg, transcript, latest_turn_started_at(transcript_path))
        if earlier:
            log(f"EARLIER_EVIDENCE: {len(earlier)} line(s)")
            transcript = f"{transcript}\n\nEARLIER IN THIS SESSION (tool output / peer messages before this turn, newest first):\n" + redact_sensitive("\n".join(f"- {row}" for row in earlier))
        if sensitive:
            log(f"SENSITIVE_PATHS: {', '.join(sensitive)}")
            transcript = f"{sensitive_section(sensitive)}\n\n{transcript}"
        committed = turn_commits_diff(repo_cwd, since)
        edited = edited_paths(entries)
        diff = "\n\n".join(x for x in (committed, snapshot) if x)[:DIFF_CAP + 2_000]
        if not diff and edited:   # nothing differs from HEAD now: the Edit-tool paths against the turn's starting commit
            diff = turn_diff(repo_cwd, edited, since=since)
        if committed:
            log(f"TURN_COMMITS: {len(turn_commits(repo_cwd, since))} commit(s), {len(committed)} chars")
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
    if sensitive and verdict_status(verdict_text) == "PASS":
        warnings = pass_warnings(verdict_text)
        if warnings:
            # On auth/secrets/payment surfaces a warning is a finding. Upgrade deterministically.
            log(f"SENSITIVE_UPGRADE: {len(warnings)} warning(s) -> ISSUES on {', '.join(sensitive)}")
            verdict_text = "VERDICT: ISSUES\n" + "\n".join(f"{i}. {w}" for i, w in enumerate(warnings, 1))

    if verdict_text and not valid_verdict(verdict_text):
        try:
            repaired = repair_verdict_format(verdict_text)
            if valid_verdict(repaired) and verdict_status(repaired) == "ISSUES":   # a repair may block, never approve
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

    if verdict_status(verdict_text) == "PASS":
        save_verdict(session_id, turn_id, "PASS", verdict_text)
        reset_consecutive_blocks(session_id)
        ledger.clear(session_id)
        allow_stop("audit passed")
        return

    if verdict_status(verdict_text) == "ISSUES":
        log_noise_classes(verdict_text)
        log(f"VERDICT_FULL: {redact_sensitive(verdict_text)[:1500]!r}")   # so a block can be replayed offline
        verdict_text, unanchored = filter_unanchored_findings(verdict_text, transcript)
        verdict_text, dropped = filter_void_findings(verdict_text)
        dropped = unanchored + dropped
        verdict_text, wording = filter_wording_findings(
            verdict_text, not edited_paths(entries) and not (turn_commits(repo_cwd, since) or snapshot))
        if wording:   # advisory only: shown in the lane's transcript, never re-prompts
            _HANDOFF["text"] = (f"{_HANDOFF.get('text') or ''}\nReviewer wording advisory (not blocking): "
                                + " | ".join(w[:160] for w in wording[:3])).strip()
        if wording and verdict_status(verdict_text) == "PASS":
            log(f"WORDING_ONLY: all {len(wording)} finding(s) are wording; verdict -> PASS")
            save_verdict(session_id, turn_id, "PASS", verdict_text)
            reset_consecutive_blocks(session_id)
            ledger.clear(session_id)
            allow_stop("audit passed (only wording findings)")
            return
        if dropped and verdict_status(verdict_text) == "PASS":
            log(f"VOID_ONLY: all {len(dropped)} finding(s) void; verdict -> PASS")
            save_verdict(session_id, turn_id, "PASS", verdict_text)
            reset_consecutive_blocks(session_id)
            ledger.clear(session_id)
            allow_stop("audit passed (only void findings)")
            return
    if verdict_status(verdict_text) == "ISSUES":
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

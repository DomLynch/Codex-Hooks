"""Pure checks on the reviewer's verdict text: normalizing the first line, validity, and which findings are void."""
from __future__ import annotations

import re
import unicodedata


_FIRST_LINE_RE = re.compile(r"(?:VERDICT\s*:\s*)?(PASS|ISSUES)((?:\s*[.:](?:\s+.*)?|\s+[-\u2013\u2014(].*)?)", re.IGNORECASE)
_NO_ISSUES_RE = re.compile(r"\b(?:no|zero|without|0)\s+(?:\w+\s+)?issues\b", re.IGNORECASE)


def _names_issues(text: str) -> bool:
    """The word ISSUES in any case or form (full-width letters, bold, a label) other than 'no issues' prose."""
    plain = re.sub(r"[_*`~]", " ", unicodedata.normalize("NFKC", text))   # bold / italic / code markers are not part of the word
    return bool(re.search(r"\bissues\b|\bissue\s*:", _NO_ISSUES_RE.sub("", plain), re.IGNORECASE))   # 'ISSUE:' too; prose 'a minor issue' stays


def normalize_verdict(text: str) -> str:
    """'PASS' / 'PASS.' / 'VERDICT: PASS' on the FIRST line become 'VERDICT: PASS' plus the body. Any other first
    line (a disclaimer before an example, a PASS-or-ISSUES template) is returned unchanged and so is not a verdict;
    a short remark after the status ('VERDICT: PASS - no issues found', 'PASS (all good)') is dropped:
    searching the body for a status line promoted 'I could not review... VERDICT: PASS' to approval."""
    stripped = (text or "").lstrip("\ufeff").strip()
    first_line, _, rest = stripped.partition("\n")
    match = _FIRST_LINE_RE.fullmatch(first_line.strip())
    if not match or _names_issues(match.group(2)):   # a remark after the status may not name another status
        return stripped
    return f"VERDICT: {match.group(1).upper()}" + (f"\n{rest.strip()}" if rest.strip() else "")


def verdict_status(text: str) -> str:
    """The one status enum: 'PASS', 'ISSUES' or '' (anything else). The first line must be exactly
    'VERDICT: PASS' or 'VERDICT: ISSUES' (a prefix match accepted 'VERDICT: PASSIVE REVIEW ONLY' as PASS). A PASS may
    not name ISSUES anywhere after line 1, in any form ('Example of a bad reply: VERDICT: ISSUES', '**VERDICT: ISSUES**',
    'Overall: ISSUES', 'ISSUES:'); numbered or bulleted Warnings after a PASS are the designed 'PASS with Warnings'
    shape and stay. An ISSUES that also says PASS stays ISSUES, because a blocked stop is the safe side."""
    first_line, _, rest = (text or "").strip().partition("\n")
    status = first_line.strip().rpartition(" ")[2] if first_line.strip() in ("VERDICT: PASS", "VERDICT: ISSUES") else ""
    return "" if status == "PASS" and _names_issues(rest) else status


def valid_verdict(text: str) -> bool:
    status = verdict_status(text)
    if status == "PASS":
        return True
    return status == "ISSUES" and bool(re.search(r"(?m)^\s*\d+[.)]\s+\S", text))


ADVISORY_RE = re.compile(r"\b(confirm|ensure|verify|double-check|should be verified|before treating)\b", re.IGNORECASE)
ELISION_RE = re.compile(
    r"\b(elided|elision|partially[- ]elided|without showing (the )?(corresponding |actual )?(tool |run |command )?output"
    r"|no visible (execution|tool|log|run|command)?[ /]*(log )?output|not (shown|visible) in (this |the )?slice)\b", re.IGNORECASE)


# A finding that names any of these is asserting a defect, whatever else it says.
DEFECT_RE = re.compile(
    r"\b(bug|error|fails?|failed|failure|failing|missing|wrong|incorrect|broken|mismatch|regress\w*|contradict\w*|"
    r"unresolved|weakens?|removed|deleted|leak\w*|crash\w*|race|null|undefined|inconsistent|stale|silent\w*|"
    r"pending|not run|never ran|no test|untested|unsafe|secret|credential|production|misrepresent\w*|overstat\w*|false|"
    r"as fact|not (yet )?(live|merged|deployed|applied)|unsupported|only shows|does not show|doesn't show|"
    r"no tool call|never (ran|called|executed|applied)|already authorized|migration|database|schema)\b", re.IGNORECASE)


def void_finding(text: str) -> str:
    """Why this numbered finding is void ('' if it stands). Deterministic, word-boundary only.
    Elision-based: the finding's basis is that output is elided here, and it names no contradiction.
    Advisory: 'confirm/ensure/verify' with no defect named at all. Everything else stands."""
    if ELISION_RE.search(text) and not DEFECT_RE.search(text):
        return "elision-only"
    if ADVISORY_RE.search(text) and not DEFECT_RE.search(text):
        return "advisory-only"
    return ""


QUOTE_RE = re.compile(r"\[quote:\s*[\"“']([^\"”']{3,400})[\"”']\s*\]", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

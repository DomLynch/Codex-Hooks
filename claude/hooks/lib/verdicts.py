"""Pure checks on the reviewer's verdict text: normalizing the first line, validity, and which findings are void."""
from __future__ import annotations

import re


_FIRST_LINE_RE = re.compile(r"(?:VERDICT\s*:\s*)?(PASS|ISSUES)((?:\s*[.:](?:\s+.*)?|\s+[-\u2013\u2014(].*)?)", re.IGNORECASE)
_STATUS_WORD_RE = re.compile(r"\b(PASS|ISSUES)\b")   # case-sensitive: 'no issues found' is prose, 'PASS or ISSUES' is a template
_ANY_STATUS_RE = re.compile(r"(?im)^\s*VERDICT:\s*(PASS|ISSUES)\b")


def normalize_verdict(text: str) -> str:
    """'PASS' / 'PASS.' / 'VERDICT: PASS' on the FIRST line become 'VERDICT: PASS' plus the body. Any other first
    line (a disclaimer before an example, a PASS-or-ISSUES template) is returned unchanged and so is not a verdict;
    a short remark after the status ('VERDICT: PASS - no issues found', 'PASS (all good)') is dropped:
    searching the body for a status line promoted 'I could not review... VERDICT: PASS' to approval."""
    stripped = (text or "").lstrip("\ufeff").strip()
    first_line, _, rest = stripped.partition("\n")
    match = _FIRST_LINE_RE.fullmatch(first_line.strip())
    if not match or _STATUS_WORD_RE.search(match.group(2)):   # a remark after the status may not name another status
        return stripped
    return f"VERDICT: {match.group(1).upper()}" + (f"\n{rest.strip()}" if rest.strip() else "")


def verdict_status(text: str) -> str:
    """The one status enum: 'PASS', 'ISSUES' or '' (anything else). The first line must be exactly
    'VERDICT: PASS' or 'VERDICT: ISSUES' (a prefix match accepted 'VERDICT: PASSIVE REVIEW ONLY' as PASS) and no
    later line may name another status."""
    first_line, _, rest = (text or "").strip().partition("\n")
    if first_line.strip() not in ("VERDICT: PASS", "VERDICT: ISSUES"):
        return ""
    status = first_line.strip().rpartition(" ")[2]
    return "" if any(m != status for m in _ANY_STATUS_RE.findall(rest)) else status


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

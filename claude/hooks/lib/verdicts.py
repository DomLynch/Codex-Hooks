"""Pure checks on the reviewer's verdict text: normalizing the first line, validity, and which findings are void."""
from __future__ import annotations

import re


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


_STATUS_LINE_RE = re.compile(r"VERDICT: (PASS|ISSUES)")


def verdict_status(text: str) -> str:
    """The one status enum: 'PASS', 'ISSUES' or '' (anything else). The first line must be exactly
    'VERDICT: PASS' or 'VERDICT: ISSUES'; a prefix match accepted 'VERDICT: PASSIVE REVIEW ONLY' as PASS."""
    first_line = (text or "").strip().partition("\n")[0].strip()
    match = _STATUS_LINE_RE.fullmatch(first_line)
    return match.group(1) if match else ""


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

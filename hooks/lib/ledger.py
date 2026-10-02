"""Track reviewer findings across Stop retries without repeated model calls."""
from __future__ import annotations

import re
from pathlib import Path

from .json_state import load_json_locked, update_json_locked

LEDGER_PATH = Path.home() / ".codex" / "state" / "audit_pending.json"
MAX_REOPENS = 1
DECISION_RE = re.compile(r"\[F(\d+)\]\s*(fixed|rejected)\s*:\s*(.+)", re.IGNORECASE)
NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s*(.+?)\s*$")
TAGGED_RE = re.compile(r"^\[F(\d+)\]\s*(.*)$")
DECISION_INSTRUCTIONS = (
    "Findings carry IDs. In your next message include one line per ID:\n"
    "  [F1] fixed: <change and confirming evidence>\n"
    "  [F1] rejected: <why this is not a defect>\n"
    "Unanswered IDs block without another model call."
)


def _state(session_id: str) -> dict:
    data = load_json_locked(LEDGER_PATH, {})
    state = data.get(session_id) if isinstance(data, dict) else None
    return state if isinstance(state, dict) else {"next_id": 1, "findings": []}


def _save(session_id: str, state: dict) -> None:
    def updater(data: dict) -> dict:
        data.pop(session_id, None)
        data[session_id] = state
        # Unresolved requirements must survive any number/order of active sessions.
        active = {key: value for key, value in data.items()
                  if any(f.get("status") in {"open", "reopened"} for f in value.get("findings", []))}
        closed = {key: value for key, value in data.items() if key not in active}
        return {**dict(list(closed.items())[-40:]), **active}

    update_json_locked(LEDGER_PATH, updater)


def clear(session_id: str) -> None:
    def updater(data: dict) -> dict:
        data.pop(session_id, None)
        return data

    update_json_locked(LEDGER_PATH, updater)


def open_findings(session_id: str) -> list[dict]:
    return [f for f in _state(session_id)["findings"] if f.get("status") in {"open", "reopened"}]


def apply_decisions(session_id: str, message: str) -> list[str]:
    state = _state(session_id)
    decisions = {int(n): (kind.lower(), why.strip()) for n, kind, why in DECISION_RE.findall(message or "")}
    unanswered = []
    for finding in state["findings"]:
        if finding.get("status") not in {"open", "reopened"}:
            continue
        decision = decisions.get(finding["n"])
        if decision:
            finding["status"], finding["reason"] = decision
        else:
            unanswered.append(finding["id"])
    if state["findings"]:
        _save(session_id, state)
    return unanswered


def register_findings(session_id: str, verdict: str) -> tuple[str, list[str]]:
    state = _state(session_id)
    by_number = {finding["n"]: finding for finding in state["findings"]}
    lines, escalated = [], []
    for line in (verdict or "").splitlines():
        match = NUMBERED_RE.match(line)
        if not match:
            lines.append(line)
            continue
        body = match.group(1)
        tagged = TAGGED_RE.match(body)
        if tagged and int(tagged.group(1)) in by_number:
            finding = by_number[int(tagged.group(1))]
            body = tagged.group(2) or finding["text"]
            if finding.get("status") == "rejected":
                finding["reopens"] = int(finding.get("reopens", 0)) + 1
                if finding["reopens"] > MAX_REOPENS:
                    finding["status"] = "escalated"
                    escalated.append(finding["id"])
                    continue
            finding["status"], finding["text"] = "reopened", body
        else:
            number = state["next_id"]
            state["next_id"] += 1
            finding = {"n": number, "id": f"F{number}", "text": body, "status": "open", "reopens": 0}
            state["findings"].append(finding)
            by_number[number] = finding
        lines.append(f"{sum(bool(NUMBERED_RE.match(item)) for item in lines) + 1}. [{finding['id']}] {body}")
    _save(session_id, state)
    return "\n".join(lines), escalated


def prompt_section(session_id: str) -> str:
    findings = _state(session_id)["findings"]
    if not findings:
        return ""
    lines = ["PRIOR FINDINGS AND AGENT DECISIONS:"]
    for finding in findings:
        reason = f" - {finding['reason']}" if finding.get("reason") else ""
        lines.append(f"[{finding['id']}] {finding['status']}{reason} :: {finding['text'][:160]}")
    lines.append("Verify claimed fixes against evidence. Accept a rejection unless clearly wrong. "
                 "Re-raise an old finding as '1. [F2] ...'; number new findings normally.")
    return "\n".join(lines)

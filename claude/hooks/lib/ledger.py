"""Finding ledger for the Stop audit.

Pins every reviewer finding to a stable ID (F1, F2, ...) that survives across Stop
rounds in a session. The agent must answer each ID with `[Fn] fixed: ...` or
`[Fn] rejected: ...`. Unanswered IDs block without a model call. A rejected finding
may be re-raised by the reviewer at most MAX_REOPENS times; after that it is marked
`escalated` and no longer blocks — a single finding cannot be argued forever.
"""
from __future__ import annotations

import re
from pathlib import Path

from .json_state import load_json_locked, update_json_locked

LEDGER_PATH = Path.home() / ".claude" / "state" / "audit_pending.json"
MAX_REOPENS = 1
DECISION_RE = re.compile(r"\[F(\d+)\]\s*(fixed|rejected)\s*:\s*(.+)", re.IGNORECASE)
NUMBERED_RE = re.compile(r"^\s*\d+\.\s*(.+?)\s*$")
TAGGED_RE = re.compile(r"^\[F(\d+)\]\s*(.*)$")

DECISION_INSTRUCTIONS = (
    "Findings carry IDs. In your next message include exactly one line per ID:\n"
    "  [F1] fixed: <what you changed and the evidence shown>\n"
    "  [F1] rejected: <why it is not a defect>\n"
    "Unanswered IDs block without further review."
)


def _state(session_id: str) -> dict:
    data = load_json_locked(LEDGER_PATH, {})
    s = data.get(session_id) if isinstance(data, dict) else None
    return s if isinstance(s, dict) else {"next_id": 1, "findings": []}


def _save(session_id: str, state: dict) -> None:
    def updater(data: dict) -> dict:
        data[session_id] = state
        if len(data) > 40:
            data = dict(list(data.items())[-40:])
        return data

    update_json_locked(LEDGER_PATH, updater)


def clear(session_id: str) -> None:
    def updater(data: dict) -> dict:
        data.pop(session_id, None)
        return data

    update_json_locked(LEDGER_PATH, updater)


def open_findings(session_id: str) -> list[dict]:
    return [f for f in _state(session_id)["findings"] if f.get("status") in ("open", "reopened")]


def apply_decisions(session_id: str, final_message: str) -> list[str]:
    """Record the agent's [Fn] fixed/rejected lines. Returns IDs still unanswered."""
    state = _state(session_id)
    if not state["findings"]:
        return []
    decided = {int(n): (kind.lower(), why.strip()) for n, kind, why in DECISION_RE.findall(final_message or "")}
    unanswered: list[str] = []
    for f in state["findings"]:
        if f.get("status") not in ("open", "reopened"):
            continue
        d = decided.get(f["n"])
        if d:
            f["status"], f["reason"] = d
        else:
            unanswered.append(f["id"])
    _save(session_id, state)
    return unanswered



def release(session_id: str, ids: list[str]) -> None:
    """Close findings the agent never answered. 2026-10-08 (Dom, Optimizer item 5b): noted once, never re-blocking."""
    state = _state(session_id)
    for f in state["findings"]:
        if f["id"] in ids and f.get("status") in ("open", "reopened"):
            f["status"] = "released"
    _save(session_id, state)


def rejected_texts(session_id: str) -> list[str]:
    return [f["text"] for f in _state(session_id)["findings"] if f.get("status") == "rejected"]


def answered_texts(session_id: str) -> list[str]:
    """Findings the lane answered with [Fn] fixed: or rejected:. 2026-10-16 (Strategy): an answered finding is no longer a lesson."""
    return [f["text"] for f in _state(session_id)["findings"] if f.get("status") in ("fixed", "rejected")]


def register_findings(session_id: str, verdict_text: str) -> tuple[str, list[str]]:
    """Assign IDs to an ISSUES verdict's numbered lines; persist; drop over-argued ones.

    Returns (verdict_with_ids, escalated_ids). If every finding was escalated the
    returned verdict has no numbered lines — the caller should treat that as release.
    """
    state = _state(session_id)
    by_n = {f["n"]: f for f in state["findings"]}
    out_lines, escalated = [], []
    for line in (verdict_text or "").splitlines():
        m = NUMBERED_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        body = m.group(1)
        t = TAGGED_RE.match(body)
        if t and int(t.group(1)) in by_n:
            f = by_n[int(t.group(1))]
            body = t.group(2) or f["text"]
            if f.get("status") == "rejected":
                f["reopens"] = int(f.get("reopens", 0)) + 1
                if f["reopens"] > MAX_REOPENS:
                    f["status"] = "escalated"
                    escalated.append(f["id"])
                    continue
            f["status"], f["text"] = "reopened", body
        else:
            n = state["next_id"]
            state["next_id"] = n + 1
            f = {"n": n, "id": f"F{n}", "text": body, "status": "open", "reopens": 0}
            state["findings"].append(f)
            by_n[n] = f
        out_lines.append(f"{len([x for x in out_lines if NUMBERED_RE.match(x)]) + 1}. [{f['id']}] {body}")
    _save(session_id, state)
    return "\n".join(out_lines), escalated


def prompt_section(session_id: str) -> str:
    """Prior findings + agent decisions, for the reviewer. Empty on a fresh session."""
    fs = _state(session_id)["findings"]
    if not fs:
        return ""
    lines = ["PRIOR FINDINGS AND THE AGENT'S DECISIONS (earlier rounds this session):"]
    for f in fs:
        st = f.get("status", "open")
        why = f" — {f['reason']}" if f.get("reason") else ""
        lines.append(f"[{f['id']}] {st}{why} :: {f['text'][:160]}")
    lines.append(
        "Verify each 'fixed' against evidence in the slice. Accept a 'rejected' unless the reason is "
        "clearly wrong. When re-raising a prior finding write its ID: '1. [F2] ...'. New findings get "
        f"new numbers from F{_state(session_id)['next_id']}."
    )
    return "\n".join(lines)

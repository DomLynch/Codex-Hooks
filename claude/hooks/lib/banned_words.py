"""Banned words for engine-slice work (Dom via Strategy, 2026-10-09). Log only, never a block."""
from __future__ import annotations

import re

ENGINE_PATH_RE = re.compile(r"src/fight/|origins/combat/")
# The fixtures that must never hit, removed before matching ("the Pit's live circle" is a wall, not a code move).
ALLOWED_RE = re.compile(r"the Pit client|Pit-only|the Pit's live circle", re.IGNORECASE)
BANNED_RE = re.compile(
    r"\b(cop(?:y|ies|ied|ying)|port(?:ed|ing)?(?!\s*\d)|build(?:s|ing)?|built|rebuil\w*|implement\w*)\b"
    r"|the Pit's\b|Pit-side feature", re.IGNORECASE)
SEND_TOOLS = ("SendMessage", "mcp__ccd_session_mgmt__send_message")


def banned_engine_words(entries: list) -> list[str]:
    """Banned words in this turn's outgoing lane messages that touch src/fight/ or origins/combat/."""
    hits = []
    for role, body in entries:
        if role != "TOOL CALL" or not body.startswith(SEND_TOOLS) or not ENGINE_PATH_RE.search(body):
            continue
        text = ALLOWED_RE.sub(" ", body)
        hits += [m.group(0) for m in BANNED_RE.finditer(text)]
    return hits

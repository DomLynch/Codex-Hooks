"""Is this session ready to clear itself? Three checks, reported never enforced.

Past WARN_TOKENS a lane should hand off at its NEXT task boundary and clear (Dom, 2026-09-22). The failure modes
are known and both silent: a state file written but never pushed, and a memory file written under a key the
restarted session will not read. So the Stop hook reports what is missing and, when all three hold, tells the
model to call mcp__ccd_session_mgmt__clear_session with "self". It never blocks the stop: a hook that demanded a
pushed commit could trap a lane with nothing to commit, no network or a protected branch, which is the 9-block
loop this repo fixed earlier the same day.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from lib.context_size import WARN_TOKENS, context_tokens, lane_name

TASK_CLOSED_MARKERS = ("merged", "handed over", "handing over", "task closed", "shipped", "done for now",
                       "wrapped up", "handed back", "closing out", "that closes")


def _git(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def state_file_pushed(cwd: Path, lane: str) -> tuple[bool, str]:
    """Today's docs/state/<lane>.md committed AND present on a pushed ref."""
    rel = f"docs/state/{lane}.md"
    if not (cwd / rel).exists():
        return False, f"{rel} does not exist"
    if _git(["status", "--porcelain", "--", rel], cwd):
        return False, f"{rel} has uncommitted changes"
    sha = _git(["log", "-1", "--format=%H", "--", rel], cwd)
    if not sha:
        return False, f"{rel} is not committed"
    when = _git(["log", "-1", "--format=%cs", "--", rel], cwd)
    if when != datetime.now().strftime("%Y-%m-%d"):
        return False, f"{rel} was last committed {when or 'never'}, not today"
    if not _git(["branch", "-r", "--contains", sha], cwd):
        return False, f"{rel} is committed ({sha[:7]}) but that commit is not on any remote branch - push it"
    return True, f"{rel} committed {sha[:7]}, on a remote branch"


def memory_written_today(cwd: Path) -> tuple[bool, str]:
    """A memory file written today under the key THIS session reads, and indexed in MEMORY.md."""
    key = Path.home() / ".claude" / "projects" / ("-" + str(cwd).replace("/", "-").lstrip("-")) / "memory"
    try:
        resolved = key.resolve()
        files = [p for p in resolved.iterdir() if p.suffix == ".md" and p.name != "MEMORY.md"]
    except OSError:
        return False, f"no memory directory at {key}"
    today = datetime.now().strftime("%Y-%m-%d")
    fresh = [p for p in files if datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") == today]
    if not fresh:
        return False, f"no memory file written today under {key}"
    index = resolved / "MEMORY.md"
    text = index.read_text(errors="ignore") if index.exists() else ""
    # At least one of today's memories must be indexed. Requiring ALL of them fails on files merely copied or
    # touched today (carry_memory does that), which is not the failure this check is for.
    indexed = [p.name for p in fresh if p.name in text]
    if not indexed:
        return False, (f"{len(fresh)} memory file(s) touched today under {key.name} but none are in MEMORY.md - "
                       "write the handoff memory and add its index line")
    note = "" if len(indexed) == len(fresh) else f" ({len(fresh) - len(indexed)} other file(s) touched but unindexed)"
    return True, f"{len(indexed)} memory file(s) written and indexed today under {key.name}{note}"


def task_closed(last_message: str) -> bool:
    return any(m in (last_message or "").lower() for m in TASK_CLOSED_MARKERS)


def handoff_notice(transcript_path: str, cwd: Path, last_message: str) -> str:
    """The line(s) to append to a Stop turn. '' when this does not apply."""
    tokens = context_tokens(transcript_path)
    if tokens < WARN_TOKENS or not task_closed(last_message):
        return ""
    lane = lane_name(str(cwd))
    ok_state, why_state = state_file_pushed(cwd, lane)
    ok_mem, why_mem = memory_written_today(cwd)
    head = f"# Handoff check — context {tokens // 1000}k and this turn closed a task"
    if ok_state and ok_mem:
        sid = Path(transcript_path).stem
        return (f"{head}\nBackup verified: {why_state}; {why_mem}.\n"
                "Confirm the memory key above matches the memory path in your own system prompt, then, IN THIS ORDER:\n"
                f'  1. message Lead: "clearing now, wake me: {sid}" — a cleared session is dark until something '
                "prompts it, so ask for the wake-up BEFORE you go.\n"
                '  2. call mcp__ccd_session_mgmt__clear_session with session_id "self" as the last thing you do.\n'
                "It fires when the turn ends; the old conversation stays recoverable via Resume previous session.")
    missing = [w for ok, w in ((ok_state, why_state), (ok_mem, why_mem)) if not ok]
    return (f"{head}\nNOT ready to clear — {'; '.join(missing)}.\n"
            f"Write docs/state/{lane}.md (Now / Done today / Open / Gotchas), commit and push it, write today's "
            "memory with its MEMORY.md line, then clear on the next task close. Not blocking you.")

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

from lib.context_size import context_tokens, lane_name, warn_tokens

TASK_CLOSED_MARKERS = ("merged", "handed over", "handing over", "task closed", "shipped", "done for now",
                       "wrapped up", "handed back", "closing out", "that closes")


def _git(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# A lane's doc is not always named after its worktree. Lead's is docs/state/lead.md, referenced by AGENTS.md and
# by every handoff entry in it, so the check maps the name rather than asking anyone to rename the file.
def project_slug(cwd) -> str:
    """The app's folder name for a cwd: every "/" AND every space becomes "-".

    The Stats lane sat in "~/Developer/Frankendom - Stats"; replacing only "/" produced a key with spaces
    still in it, which matches no directory, so its receipt named an empty store while its real one was
    elsewhere. The app writes "-Users-...-Frankendom---Stats", i.e. spaces are hyphens too.
    """
    return str(cwd).replace("/", "-").replace(" ", "-").lstrip("-")


STATE_DOC_ALIASES = {"lead-catalogue": "lead", "presentation": "world", "char": "character"}
TRUNK = "origin/codex/01a09a76/task-1"


def state_file_pushed(cwd: Path, lane: str, fetch: bool = True) -> tuple[bool, str]:
    """Today's docs/state/<lane>.md on TRUNK, not in this worktree.

    Reading the worktree was wrong and both Lead and I were bitten by it within an hour on 2026-09-22: a doc
    merged to trunk is absent from every worktree that has not pulled, so the check reported four lanes missing
    a file that existed. Trunk is the shared truth, and the receipt names the sha it read so a stale answer is
    visible rather than silent.
    """
    doc = STATE_DOC_ALIASES.get(lane, lane)
    rel = f"docs/state/{doc}.md"
    if fetch:   # the prompt path skips it: its hook has a 10 s budget, and a stale ref only under-reports ready
        _git(["fetch", "-q", "origin"], cwd)
    head = _git(["rev-parse", "--short", TRUNK], cwd)
    if not head:
        return False, f"cannot read {TRUNK} from {cwd} - check the remote"
    if not _git(["cat-file", "-e", f"{TRUNK}:{rel}"], cwd) and subprocess.run(
            ["git", "cat-file", "-e", f"{TRUNK}:{rel}"], cwd=str(cwd), capture_output=True).returncode != 0:
        return False, f"{rel} is not on trunk ({head}) - write it, commit it and get it merged"
    when = _git(["log", "-1", "--format=%cs", TRUNK, "--", rel], cwd)
    if when != datetime.now().strftime("%Y-%m-%d"):
        return False, f"{rel} is on trunk ({head}) but last updated {when or 'unknown'}, not today"
    sha = _git(["log", "-1", "--format=%h", TRUNK, "--", rel], cwd)
    return True, f"{rel} on trunk {head}, updated today by {sha}"


def memory_written_today(cwd: Path) -> tuple[bool, str]:
    """A memory file written today under the key THIS session reads, and indexed in MEMORY.md."""
    key = Path.home() / ".claude" / "projects" / ("-" + project_slug(cwd)) / "memory"
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


def _readiness(cwd: Path, lane: str, head: str, fetch: bool = True) -> str:
    """Both backup checks, then either the clear-yourself steps or what is still missing."""
    ok_state, why_state = state_file_pushed(cwd, lane, fetch)
    ok_mem, why_mem = memory_written_today(cwd)
    doc = STATE_DOC_ALIASES.get(lane, lane)
    if ok_state and ok_mem:
        return (f"{head}\nBackup verified: {why_state}; {why_mem}.\n"
                "When the task in hand closes (never mid-task), clear this session YOURSELF. It is your job, not "
                "Dom's: do not ask Dom to run /clear. Confirm the memory key above matches the memory path in your "
                "own system prompt, then as the last steps of that turn, IN THIS ORDER:\n"
                f'  1. message Lead: "clearing now, wake me: the {lane} lane" — a cleared session is dark until '
                "something prompts it, so ask for the wake-up BEFORE you go. Ask by LANE TITLE: the transcript id "
                "this hook can see is not the id send_message resolves (Lead, 2026-09-22: it returns session not "
                "found). The Lead lane messages Strategy instead.\n"
                '  2. call mcp__ccd_session_mgmt__clear_session with session_id "self" as the last thing you do.\n'
                "It fires when the turn ends; the old conversation stays recoverable via Resume previous session.")
    missing = [w for ok, w in ((ok_state, why_state), (ok_mem, why_mem)) if not ok]
    return (f"{head}\nNOT ready to clear — {'; '.join(missing)}.\n"
            f"At the close of the task in hand, write docs/state/{doc}.md (Now / Done today / Open / Gotchas), "
            "commit and push it, write today's memory with its MEMORY.md line; this check then tells you to clear "
            "yourself. Not blocking you.")


def prompt_handoff_notice(transcript_path: str, cwd: Path) -> str:
    """The readiness report for a UserPromptSubmit context. '' below the lane's threshold or outside a lane.

    This is the copy the model actually reads. The Stop-hook copy below goes to stderr on an allowed stop, which
    Claude Code stores (attachment.stderr) but never shows the model (attachment.content is ""), so from
    2026-09-22 to 2026-09-24 no lane ever saw a "ready" notice: Lead and Strategy cleared from their own memory
    rules, and World and Executioner, told only "/clear" by the context line, asked Dom to do it.
    """
    lane = lane_name(str(cwd))
    tokens = context_tokens(transcript_path)
    if lane == "session" or tokens < warn_tokens(lane):
        return ""
    return _readiness(cwd, lane, f"# Handoff check — context {tokens // 1000}k", fetch=False)


def handoff_notice(transcript_path: str, cwd: Path, last_message: str) -> str:
    """The Stop-turn copy: logged as HANDOFF_CHECK ready/not ready. See prompt_handoff_notice for why the model
    never reads it. '' when this does not apply."""
    tokens = context_tokens(transcript_path)
    lane = lane_name(str(cwd))
    if tokens < warn_tokens(lane) or not task_closed(last_message):
        return ""
    return _readiness(cwd, lane, f"# Handoff check — context {tokens // 1000}k and this turn closed a task")

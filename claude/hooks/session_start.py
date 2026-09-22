#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.autonomy import remember_repo
from lib.api import exit_if_audit_child
from lib.log import log
from lib.git import is_git_repo
from lib.quality import mine_backlog
from lib.state import load_handoff, now_iso, write_handoff, write_session_spec
from lib.claude_adapter import latest_task_slice_claude as latest_task_slice
from user_prompt_submit import COMPLETION_POLICY, RELEASE_POLICY, SENTRY_POLICY


COMPONENT = "session_start"

SESSION_BASELINE = """# V4/MCP Session Baseline
- Project/code/repo work: use knowledge MCP `get_playbook()` when available; read local `AGENTS.md` and `PROJECT_STATE.md` when present.
- Route tools deliberately: knowledge MCP for doctrine, GitHub for PR/CI, Browser for UI/local web, official docs/web for current facts.
- Mandatory before non-trivial edits: use Semble and Codegraph together, not as alternatives; run three materially different Semble searches, then Codegraph MCP for structural context/impact. If skipping either, state why.
- If Codegraph is unavailable, retry once; only then degrade to Semble + `rg` and state the fallback in the receipt.
- Before stopping, give a concise coverage receipt for relevant entry points, state/config, retries/fallbacks/background work, tests, and post-edit impact/index freshness.
- Keep command output lean: targeted commands, capped output, summarized failures, narrow `rg` paths.
- No full diffs/logs/generated files/archives/dependency trees/repeated output unless needed.
- Avoid duplicate parallel exploration. Split scout, surgeon, reviewer with non-overlapping scope.
- Start project work from a compact context packet: task, known files, do-not-inspect paths, evidence, success checks.
- Scale V4 loop to task: success condition, likely files, smallest reversible change, strong verification.
- Prefer minimal LOC: delete/refactor before adding; avoid hardcoded or silo-specific fixes unless scoped.
- Before stopping, run self-review plus strongest relevant automated/judge check.
- Code changes must pass the repository .quality-gate.json commands; missing tools and failed checks are blockers, not skips.
- Match debugging evidence to the failure: reproducer/regression, ast-grep patterns, Hypothesis/randomized state tests, VizTracer/py-spy performance traces, bounded mutation tests; Tree-sitter for disputed graph edges and secret-free Repomix for handoffs. Require applicable evidence or an explicit unavailable reason, not every tool on every edit.
- Keep context on current task; avoid old-history trawling unless required.
- Default to autonomous execution. Do not ask for confirmation when a reasonable reversible default exists.
- Missing detail: choose conservative project-local default, state briefly, keep working.
- Existing authorization covers ordinary necessary steps within scope, including a requested deployment; do not ask for the same permission twice.
- Ask only for true blockers: missing inputs after checking approved sources, or unapproved destructive/irreversible actions, purchases, regulated decisions and high-blast-radius scope choices.
- Apply and verify simple reversible fixes within scope. Finish the authorized outcome, including requested deployment/live checks, not just local tests.
- If blocked, finish independent authorized work and report the failed check and exact missing input after checking approved sources; never expose secrets or bypass safety checks.
- Explicit stop/pause, audit-only, plan-only and status-only requests override persistence. Do not start unrelated backlog tasks; retry exhaustion is unresolved work, not success.
- Long runs: bounded reversible progress with logs/checkpoints over micro-management.
"""
SESSION_BASELINE += RELEASE_POLICY + COMPLETION_POLICY + SENTRY_POLICY + "\n"

BACKLOG_SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "archive",
    "archived",
    "legacy",
    "agent_archived",
    "agent_legacy",
}


def additional_context(context: str) -> None:
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


def format_briefing(handoff: dict, mined: list[dict]) -> str:
    lines = [SESSION_BASELINE.rstrip(), "", "# Session Handoff"]
    codegraph = codegraph_notice(handoff.get("cwd"))
    if codegraph:
        lines.extend(["", codegraph])
    if handoff.get("last_updated_at"):
        lines.append(f"Previous session updated: {handoff['last_updated_at']}")
    last_task = handoff.get("last_task") or {}
    if last_task:
        desc = last_task.get("description") or "Unnamed task"
        pr_url = last_task.get("pr_url") or ""
        lines.append(f"Last completed: {desc}" + (f" ({pr_url})" if pr_url else ""))
    backlog = handoff.get("queued_backlog") or mined
    if backlog:
        lines.append("Queued backlog:")
        for i, item in enumerate(backlog[:5], 1):
            lines.append(
                f"{i}. {item.get('description', 'Task')} "
                f"[{item.get('source_file', 'unknown')} · p{item.get('priority', 0)}]"
            )
        lines.append("Use the top item if the user has not provided a more specific task.")
    return "\n".join(lines)


def codegraph_notice(cwd: str | None) -> str:
    if not cwd:
        return ""
    path = Path(cwd).resolve()
    if not is_git_repo(path):
        return ""
    if (path / ".codegraph" / "codegraph.db").exists():
        return ""
    return "# Codegraph\n- No local index found for this repo; run `codegraph init -i` before structural code work."


def lane_from_identity(cwd: Path) -> str:
    """The lane name the worktree declares in its untracked CLAUDE.local.md, else the folder suffix."""
    try:
        for line in (cwd / "CLAUDE.local.md").read_text(errors="ignore").splitlines():
            if "lane" in line and "**" in line:
                parts = [p for p in line.split("**") if p.strip()]
                if len(parts) >= 2:
                    return parts[1].strip()
    except OSError:
        pass
    name = cwd.name
    return name.removeprefix("frankendom-") if name.startswith("frankendom-") else name


def now_paragraph(cwd: Path, lane: str) -> str:
    """The 'Now' section of docs/state/<lane>.md: what the next session should pick up."""
    try:
        text = (cwd / "docs" / "state" / f"{lane}.md").read_text(errors="ignore")
    except OSError:
        return ""
    lines = text.splitlines()
    # Prefer an explicit "Now" section (the handoff format we ask for); otherwise fall back to the newest
    # "## " section, which is what the lanes' existing append-style state docs actually look like.
    start, stop = None, len(lines)
    for i, line in enumerate(lines):
        if line.lstrip("#*_ ").lower().startswith("now"):
            # "**Now:** nothing in flight." is inline, not a heading, so keep the matched line itself.
            start = i
            for j in range(start + 1, len(lines)):
                if lines[j].startswith("#") or lines[j].lstrip("*_ ").lower().startswith(("done today", "open", "gotchas")):
                    stop = j
                    break
            break
    if start is None:
        heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
        if not heads:
            return ""
        start, stop = heads[0], (heads[1] if len(heads) > 1 else len(lines))
    return "\n".join(lines[start:stop]).strip()[:900]


# A pre-rehome folder key is SHARED by every lane created from it (Weapons, 2026-09-22 19:55: its system prompt
# names the same root as Strategy's). Linking one is how a lane hands its memory to every other lane, so these
# are never link targets however well their index matches.
SHARED_KEYS = ("-Users-domininclynch-Desktop-Business-frankendom", "-Users-domininclynch-Desktop-Business-Research-Agent-Bot")


def blind_restart_hint(key: Path) -> str:
    """Empty key + another key whose index is clearly THIS lane's: print the exact fix. Otherwise say nothing."""
    lane = key.parent.name.split("frankendom-")[-1].lower()
    if not lane:
        return ""
    try:   # only for a genuinely empty key - never propose relinking one that already holds memory
        if any(p.suffix == ".md" and p.name != "MEMORY.md" for p in key.resolve().iterdir()):
            return ""
    except OSError:
        pass
    for other in sorted((Path.home() / ".claude" / "projects").glob("*/memory/MEMORY.md")):
        if other.parent == key or "claude-audit" in str(other) or other.parent.parent.name in SHARED_KEYS:
            continue
        try:
            text = other.read_text(errors="ignore")
        except OSError:
            continue
        if lane not in text.lower():
            continue
        # A key whose index opens with another lane's name belongs to that lane, whatever else it mentions.
        first = next((ln for ln in text.splitlines() if ln.startswith("- ")), "").lower()
        owner = other.parent.parent.name.split("frankendom-")[-1].lower()
        if owner and owner != lane:
            continue
        if first and lane not in first and any(x in first for x in ("lane", "handoff")):
            continue
        return (f"\nAnother key holds this lane's memory: {other.parent}\n"
                f"If that is yours, link it before doing anything else:\n"
                f"  mv {key} {key}.pre-restore 2>/dev/null; ln -s {other.parent} {key}")
    return ""


def memory_receipt(cwd: Path, session_id: str = "") -> str:
    """Prove the memory actually loaded, at the top of a fresh session.

    Memory is keyed by a path, and after a re-home there are TWO of them: the key this folder implies, and the
    memory root the desktop app handed the session. On 2026-09-22 a lane's own files sat in its cwd key while the
    app pointed it at its pre-rehome root (two files), so a /clear looked like amnesia; the lane fixed it by
    symlinking the app's root at the real key. This hook can only compute the cwd key - it cannot see the app's
    root - so it prints what it can prove (resolved key, following a symlink, counts, newest files today) and asks
    the model to compare that against the memory path in its own system prompt, which only the model can read.
    Read-only, no model call.
    """
    key = Path.home() / ".claude" / "projects" / ("-" + str(cwd).replace("/", "-").lstrip("-")) / "memory"
    lane = lane_from_identity(cwd)
    try:
        resolved = key.resolve()
        files = sorted(p for p in resolved.iterdir() if p.suffix == ".md" and p.name != "MEMORY.md")
    except OSError:
        return (f"# Memory check — lane: {lane}\n"
                f"No memory directory for this folder ({key})." + (blind_restart_hint(key) or
                " If you expected a handoff here it was written from a different cwd - find it with: "
                "ls ~/.claude/projects/*/memory/ | grep -i <lane>"))
    today = datetime.now().strftime("%Y-%m-%d")
    fresh = [f.name for f in sorted((f for f in files if datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d") == today),
                                    key=lambda f: f.stat().st_mtime, reverse=True)]
    link = f" (symlink -> {resolved})" if key.is_symlink() else ""
    index = resolved / "MEMORY.md"
    lines = len([ln for ln in index.read_text(errors="ignore").splitlines() if ln.startswith("- ")]) if index.exists() else 0
    written = (f"; {len(fresh)} written today, newest: {', '.join(fresh[:3])}" if fresh else "; none written today")
    # A cleared session is dark until something prompts it, so it must ask for a wake-up before it goes. It cannot
    # hand over an id: the hook only sees the transcript id (8e9d31a7-...), while the app's send_message resolves
    # a different one (local_ea03a6d4-...). Lead proved that the hook's id returns "session not found" and that it
    # resolves lanes by title instead, so the line names the lane, not an id that fails silently.
    me = "\nTo be woken after a clear, ask by LANE TITLE, not by id: the id in this hook is the transcript id and "\
         "send_message will not resolve it."
    identity = "" if (cwd / "CLAUDE.local.md").is_file() else (
        "\nNo CLAUDE.local.md in this worktree: the standing context/handoff rule is NOT loaded here. "
        "Ask Hooks to run scripts/lane-identity.mjs for this lane.")
    brief = now_paragraph(cwd, lane)
    brief = f"\n\n## Now (from docs/state/{lane}.md)\n{brief}" if brief else ""
    empty = blind_restart_hint(key) if not files else ""
    return (f"# Memory check — lane: {lane}\nKey: {key}{link}\n"
            f"{len(files)} memory file(s), {lines} index line(s){written}{me}{empty}{identity}\n"
            "Compare this key with the memory path in your own system prompt. If they differ, READ THIS KEY "
            "DIRECTLY and write your memory here: the system-prompt path is a SHARED pre-rehome root used by every "
            "lane created from that folder, so do not symlink or copy into it - that would hand your memory to the "
            "other lanes and take theirs.\n"
            "If you have just restarted, read today's files before answering and say which ones you loaded."
            + brief +
            "\n\nLast resort if this is all wrong: the app's 'Resume previous session' restores the old conversation, "
            "and hooks/lib/export_transcript.py turns its .jsonl into readable markdown.")


def handoff_for_cwd(handoff: dict, cwd: Path) -> dict:
    if not handoff:
        return {}
    handoff_cwd = handoff.get("cwd")
    if handoff_cwd and Path(handoff_cwd).resolve() == cwd:
        return handoff
    # Legacy handoff files had no cwd and can leak unrelated backlog into a new
    # project session. Keep the baseline, drop the stale handoff.
    return {}


def filter_backlog(items: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for item in items or []:
        source = str(item.get("source_file") or "")
        parts = set(Path(source.split(":", 1)[0]).parts)
        if parts & BACKLOG_SKIP_PARTS:
            continue
        if any(part.startswith(".") for part in parts):
            continue
        if "Add coverage for Python function `test_" in str(item.get("description") or ""):
            continue
        filtered.append(item)
    return filtered


def main() -> None:
    exit_if_audit_child()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = payload.get("session_id") or "unknown"
        cwd = Path(payload.get("cwd") or ".").resolve()
        prompt = (
            payload.get("prompt")
            or payload.get("initial_prompt")
            or payload.get("last_user_message")
            or latest_task_slice(payload.get("transcript_path") or "", "")
        )
        if prompt:
            write_session_spec(session_id, prompt)
            log(COMPONENT, f"session spec captured chars={len(prompt)}")
        remember_repo(cwd)
        additional_context(memory_receipt(cwd, session_id))
        handoff = handoff_for_cwd(load_handoff(), cwd)
        if handoff.get("queued_backlog"):
            original_count = len(handoff.get("queued_backlog") or [])
            handoff["queued_backlog"] = filter_backlog(handoff.get("queued_backlog") or [])
            if len(handoff["queued_backlog"]) != original_count:
                write_handoff(handoff)
        mined: list[dict] = []
        if not handoff.get("queued_backlog") and cwd.exists() and is_git_repo(cwd):
            mined = filter_backlog(mine_backlog(cwd, max_items=10))
            if mined and not handoff:
                handoff = {
                    "session_id": session_id,
                    "cwd": str(cwd),
                    "queued_backlog": mined,
                    "tasks_completed_count": 0,
                    "session_started_at": now_iso(),
                }
                write_handoff(handoff)
        if handoff or mined:
            context = format_briefing(handoff, mined)
            log(COMPONENT, f"context injected; backlog={len(handoff.get('queued_backlog') or mined)}")
            additional_context(context)
        else:
            log(COMPONENT, "baseline context injected; no handoff/backlog")
            codegraph = codegraph_notice(str(cwd))
            additional_context(SESSION_BASELINE if not codegraph else f"{SESSION_BASELINE.rstrip()}\n\n{codegraph}")
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.api import exit_if_audit_child
from lib.autonomy import remember_repo
from lib.context_size import session_lane
from lib.git import (
    is_git_repo,
    resolve_git_cwd,
    safe_run,
)
from lib.json_state import load_json_locked, update_json_locked
from lib.log import log
from lib.rehome import is_rehome_turn
from lib.quality import (ci_covered_commit, deploy_hold, gate_busy, pending_quality, run_project_gate, run_quality_checks,
                         unchanged_from_base)
from lib.transcript import edited_paths, session_made_edits
from deploy_guard import active_lock as active_deploy_lock
from lib.claude_adapter import normalize_payload, latest_task_entries_claude, latest_task_slice_claude
from lib.audit_state import retry_turn_id


COMPONENT = "stop_orchestrator"
MILESTONE_INTENT_PATTERNS = (
    re.compile(r"\bready\s+to\s+(?:merge|ship|release|deploy)\b", re.IGNORECASE),
    re.compile(r"\b(?:ship|merge|land|deploy|release)\s+(?:it|this|now)\b", re.IGNORECASE),
    re.compile(r"\baaa\s+milestone\s+(?:release|ship|merge|deploy)\b", re.IGNORECASE),
)
AAA_PROTOCOL_PATH = Path.home() / ".claude" / "specs" / "aaa_protocol.md"
# session_id -> deploy revision already announced. Stop additionalContext continues the turn, so repeating the
# deferral on every Stop re-woke an idle lane until Claude Code's consecutive-block cap ended it (2026-09-23).
DEFERRAL_NOTICE_PATH = Path.home() / ".claude" / "state" / "deploy_deferral_notified.json"


# The Stop output is ONE JSON value. Helpers record notices here; block() and flush_notices() are the only writers,
# and _EMITTED stops a second object (a deferral object then a block object was invalid JSON: 'Extra data', so
# Claude Code dropped the block).
_CONTEXT_NOTES: list[str] = []     # additionalContext: continues the turn with information
_SYSTEM_MESSAGES: list[str] = []   # systemMessage: shown to the user
_EMITTED = False


def _emit(obj: dict) -> None:
    global _EMITTED
    if _EMITTED:
        return
    _EMITTED = True
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def flush_notices() -> None:
    out: dict = {}
    if _CONTEXT_NOTES:
        out["hookSpecificOutput"] = {"hookEventName": "Stop", "additionalContext": "\n".join(_CONTEXT_NOTES)}
    if _SYSTEM_MESSAGES:
        out["systemMessage"] = "\n".join(_SYSTEM_MESSAGES)
    if out:
        _emit(out)


def block(reason: str) -> None:
    notes = [*_CONTEXT_NOTES, *_SYSTEM_MESSAGES]
    _emit({"decision": "block", "reason": reason + ("\n\n" + "\n".join(notes) if notes else "")})
    sys.exit(0)


def strip_hook_prompts(text: str) -> str:
    without_xml = re.sub(
        r"<hook_prompt\b[^>]*>.*?</hook_prompt>",
        "",
        text or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    return re.sub(r"\[[^\]\n]*(?:hook|gate)[^\]\n]*\]", "", without_xml, flags=re.IGNORECASE)


def milestone_detected(text: str) -> bool:
    cleaned = strip_hook_prompts(text)
    return any(pattern.search(cleaned) for pattern in MILESTONE_INTENT_PATTERNS)


# run_aaa_gate's third outcome: not failed, not passed. Truthy so the Stop is not blocked, but the repo stays
# owed a gate (audit C, 2026-09-27).
DEFERRED = "deferred"


def defer_gate(deploying: dict, session_id: str, mid_gate: bool = False) -> str:
    # One deployer, one Mac: the repository gate (test suites, browser checks) waits its turn. The edits are
    # still gated by the PR's CI and by the next Stop after the deploy; the lane is told, not silently passed.
    # Also waits (never fails) for an overloaded Mac, a Deploy hold, or the same check already running here.
    revision = str(deploying.get('revision', ''))[:7]
    when = "stopped mid-gate" if mid_gate else "in flight"
    why = deploying.get("reason") or f"deploy {revision} is in flight on this Mac (one-deployer rule)"
    log(COMPONENT, f"repository quality gate deferred ({when}): {why}")
    key = revision or why.split(" (")[0]
    if load_json_locked(DEFERRAL_NOTICE_PATH, {}).get(session_id) == key:
        return DEFERRED
    update_json_locked(DEFERRAL_NOTICE_PATH, lambda data: {**data, session_id: key})
    _CONTEXT_NOTES.append(f"[Repository Quality Gate] deferred: {why}; "
                          "the gate runs on a later Stop, and the PR's CI still covers this change.")
    return DEFERRED


# 2026-10-09 (Dom, via Strategy and Lead): the repository suite runs on the Mac only in the Deploy lane; the other
# Frankendom lanes rely on the PR's CI (load hit 20-80 on 10 cores with 9 lanes gating at once). FAILS CLOSED: only
# a lane named here (or "<name>-something", the folder variants like combat-cu or char-fix) skips; Deploy, an unknown
# lane and a session with no lane (hooks lane, COO) all still run the gate.
SKIP_GATE_LANES = ("strategy", "lead", "code-quality", "characters", "char", "combat", "duels", "backend", "web", "world", "pit")


def gate_skipped_for(lane: str) -> bool:
    return any(lane == k or lane.startswith(k + "-") for k in SKIP_GATE_LANES)


def run_aaa_gate(cwd: Path, transcript: str, timeout: float = 420, session_id: str = "", transcript_path: str = "") -> bool | str:
    """`transcript` is the TEXT of the task slice (milestone detection); `transcript_path` is the FILE, which the lane lookup needs:
    a desktop worktree folder names no lane, so only the title inside the file does (2026-10-10: passing the text here meant
    every worktree lane failed the lookup and ran this gate on the Mac)."""
    if (cwd / ".quality-gate.json").exists():
        lane = session_lane(transcript_path, str(cwd))
        if gate_skipped_for(lane):
            log(COMPONENT, f"repository quality gate skipped: lane {lane} relies on PR CI (local gate is Deploy-only) cwd={cwd}")
            return True
        sha, ci = ci_covered_commit(cwd)
        if ci == "passed":
            # Nothing local to verify: clean tree at a commit GitHub Actions already passed.
            # Running the contract here would only duplicate CI and load the machine.
            log(COMPONENT, f"repository quality gate skipped: clean tree, CI passed on {sha}")
            return True
        if ci == "pending":
            return defer_gate({"reason": f"CI is still running on {sha} (clean tree, nothing local to check)"}, session_id)
        base = unchanged_from_base(cwd)
        if base:
            # No task-owned change here: clean tree, nothing on top of the base branch. The base is gated by its
            # own CI and by the lanes that changed it (Lead, 2026-09-27: Deploy on unchanged trunk, 9 min per Stop).
            log(COMPONENT, f"repository quality gate skipped: clean tree, no change vs {base}")
            return True
        deploying = active_deploy_lock() or gate_busy(cwd)
        if deploying:
            return defer_gate(deploying, session_id)
        # A deploy (or Deploy's hold) that starts mid-gate stops it too, so the release rows get the Mac.
        result = run_project_gate(cwd, timeout=timeout, defer_to=lambda: active_deploy_lock() or deploy_hold())
        if result.get("deferred"):
            return defer_gate(result["deferred"], session_id, mid_gate=True)
        failed = "; ".join(str(item["tool"])[:80] for item in result.get("results", []) if not item.get("ok")) or "none"
        log(COMPONENT, f'repository quality gate ok={result["ok"]} lane={lane or "session"} cwd="{cwd}" seconds={result.get("seconds", 0)} failed={failed}')
        if not result["ok"]:
            block("[Repository Quality Gate]\n\n" + "\n".join(
                f"{item['tool']}: {item['output']}" for item in result["results"] if not item["ok"]
            ))
            return False
        return True
    if not milestone_detected(transcript):
        # a repo owed for not_run stays owed on a plain Stop; only a real gate run may clear it
        return DEFERRED if load_json_locked(DEFERRAL_NOTICE_PATH, {}).get(f"not_run:{session_id}:{cwd}") else True
    protocol = ""
    try:
        protocol = AAA_PROTOCOL_PATH.read_text()
    except Exception:
        log(COMPONENT, "AAA protocol cache missing; using lean built-in gate")
    checks: list[str] = []
    rc, out, err = safe_run(["git", "diff", "--name-only"], cwd=cwd)
    changed = [cwd / line for line in out.splitlines() if line.strip()] if rc == 0 else []
    quality = run_quality_checks(changed, cwd, timeout=120) if changed else {"ok": True, "results": []}
    if quality.get("status") == "failed":
        checks.append("Quality checks failed:\n" + "\n".join(
            f"- {item['tool']} {item['file']}: {item['output'][:1000]}"
            for item in quality.get("results", []) if not item.get("ok")
        ) + ("\n" + quality["note"] if quality.get("not_run") else ""))
    if changed and not protocol:
        log(COMPONENT, "AAA gate ran with cached protocol missing")
    project_failures = run_project_aaa_checks(cwd, changed)
    checks.extend(project_failures)
    if checks:
        block("[AAA Milestone Gate]\n\n" + "\n\n".join(checks))
        return False
    if quality.get("status") == "not_run":   # not run is not passed: tell the user, keep the repo owed a gate
        _SYSTEM_MESSAGES.append("[Quality Floor] " + quality["note"])
        update_json_locked(DEFERRAL_NOTICE_PATH, lambda data: {**data, f"not_run:{session_id}:{cwd}": True})
        return DEFERRED
    log(COMPONENT, "AAA gate passed")
    return True


def package_manager_command(cwd: Path, package: dict) -> str:
    declared = str(package.get("packageManager") or "").lower()
    if declared.startswith("pnpm@") or (cwd / "pnpm-lock.yaml").exists():
        return "pnpm"
    if declared.startswith("yarn@") or (cwd / "yarn.lock").exists():
        return "yarn"
    if declared.startswith("bun@") or (cwd / "bun.lockb").exists() or (cwd / "bun.lock").exists():
        return "bun"
    return "npm"


def run_project_aaa_checks(cwd: Path, changed: list[Path] | None = None) -> list[str]:
    failures: list[str] = []
    changed = changed or []
    if not changed:
        log(COMPONENT, "AAA project checks skipped: clean worktree")
        return failures

    py_changed = [path for path in changed if path.suffix == ".py"]
    if py_changed:
        if shutil.which("ruff"):
            rc, out, err = safe_run(["ruff", "check", *[str(path.relative_to(cwd)) for path in py_changed]], cwd=cwd, timeout=120)
            if rc != 0:
                failures.append("ruff failed:\n" + (out + err)[-3000:])
        if shutil.which("mypy") and any((cwd / name).exists() for name in ("mypy.ini", "pyproject.toml", "setup.cfg")):
            rc, out, err = safe_run(["mypy", "--no-error-summary", *[str(path.relative_to(cwd)) for path in py_changed]], cwd=cwd, timeout=120)
            if rc != 0:
                failures.append("mypy failed:\n" + (out + err)[-3000:])
        test_changed = [path for path in py_changed if path.name.startswith("test_") or "tests" in path.parts]
        if shutil.which("pytest") and test_changed:
            rc, out, err = safe_run(["pytest", "-q", *[str(path.relative_to(cwd)) for path in test_changed]], cwd=cwd, timeout=120)
            if rc != 0:
                failures.append("pytest failed:\n" + (out + err)[-3000:])

    if (cwd / "package.json").exists():
        try:
            package = json.loads((cwd / "package.json").read_text())
        except Exception:
            package = {}
        scripts = package.get("scripts") or {}
        js_changed = [
            path for path in changed
            if path.suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json"} or ".github" in path.parts
        ]
        manager = package_manager_command(cwd, package)
        if js_changed and scripts.get("test:changed") and shutil.which(manager):
            rc, out, err = safe_run([manager, "run", "test:changed"], cwd=cwd, timeout=120)
            if rc != 0:
                failures.append(f"{manager} run test:changed failed:\n" + (out + err)[-3000:])
        elif js_changed and scripts.get("test") and shutil.which(manager):
            cmd = [manager, "test"]
            if manager in {"npm", "pnpm", "yarn"}:
                cmd.extend(["--", "--watch=false"])
            rc, out, err = safe_run(cmd, cwd=cwd, timeout=120)
            if rc != 0:
                failures.append(f"{manager} test failed:\n" + (out + err)[-3000:])

    if (cwd / "go.mod").exists() and shutil.which("go"):
        rc, out, err = safe_run(["go", "test", "./..."], cwd=cwd, timeout=120)
        if rc != 0:
            failures.append("go test failed:\n" + (out + err)[-3000:])
    if (cwd / "Cargo.toml").exists() and shutil.which("cargo"):
        for cmd in (["cargo", "check"], ["cargo", "test"]):
            rc, out, err = safe_run(cmd, cwd=cwd, timeout=120)
            if rc != 0:
                failures.append(f"{' '.join(cmd)} failed:\n" + (out + err)[-3000:])
    return failures


def main() -> None:
    global _EMITTED
    _EMITTED = False   # one process = one hook run; tests call main() repeatedly in-process
    _CONTEXT_NOTES.clear()
    _SYSTEM_MESSAGES.clear()
    exit_if_audit_child()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        why = is_rehome_turn(payload)
        if why:
            log(COMPONENT, f"REHOME_FAST_EXIT: {why}")
            return
        payload = normalize_payload(payload)
        session_id = payload.get("session_id") or "unknown"
        turn_id = retry_turn_id(session_id, payload, payload.get("turn_id") or "")
        cwd = Path(payload.get("cwd") or ".").resolve()
        transcript = latest_task_slice_claude(payload.get("transcript_path") or "", payload.get("last_assistant_message") or "")
        entries = latest_task_entries_claude(payload.get("transcript_path") or "")
        edited = [Path(p) for p in edited_paths(entries)]
        cwd = resolve_git_cwd(cwd, edited, transcript=transcript)
        remember_repo(cwd)

        log(COMPONENT, f"start session={session_id} turn={turn_id} transcript={len(transcript)} cwd={cwd}")
        pending = pending_quality(session_id)
        quality_deadline = time.monotonic() + 480
        deferred: list[str] = []
        for project in pending:
            target = Path(project)
            owed = load_json_locked(DEFERRAL_NOTICE_PATH, {}).get(f"not_run:{session_id}:{target}")
            if not (target / ".quality-gate.json").exists() and not owed:   # owed: deferred as not_run, re-check below
                block(f"[Repository Quality Gate] Missing {target / '.quality-gate.json'}; declare the repository quality commands before completing code changes.")
            outcome = run_aaa_gate(target, transcript, timeout=quality_deadline - time.monotonic(), session_id=session_id, transcript_path=payload.get("transcript_path") or "")
            if not outcome:
                return
            if outcome == DEFERRED:
                deferred.append(project)
            elif owed:
                update_json_locked(DEFERRAL_NOTICE_PATH, lambda data: {**data, f"not_run:{session_id}:{target}": False})
        # 2026-10-09 (Strategy): a session that made no edits owes no gate for the repo it only ran commands in.
        if str(cwd) not in pending and is_git_repo(cwd) and session_made_edits(entries):
            outcome = run_aaa_gate(cwd, transcript, timeout=quality_deadline - time.monotonic(), session_id=session_id, transcript_path=payload.get("transcript_path") or "")
            if not outcome:
                return
            if outcome == DEFERRED:
                pending_quality(session_id, cwd)  # still owed: the next Stop runs it
                deferred.append(str(cwd))
        if pending:
            pending_quality(session_id, done=[p for p in pending if p not in deferred])
        log(COMPONENT, "allow stop")
    except Exception as exc:
        log(COMPONENT, f"gate error: {exc}")
        block(f"[Repository Quality Gate] Unable to verify completion: {exc}")
    finally:
        flush_notices()


if __name__ == "__main__":
    main()

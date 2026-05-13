#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

from lib.api import call_claude, call_mimo
from lib.autonomy import pop_next_task, remember_repo
from lib.git import (
    commit,
    current_session_branch,
    ensure_branch,
    files_changed_summary,
    github_pr_preflight,
    has_worktree_changes,
    is_git_repo,
    open_pr,
    push,
    resolve_git_cwd,
    safe_run,
    task_branch,
)
from lib.json_state import load_json_locked, update_json_locked
from lib.log import log
from lib.quality import run_quality_checks
from lib.state import load_handoff, read_session_spec, write_handoff
from lib.claude_adapter import normalize_payload, latest_task_slice_claude


COMPONENT = "stop_orchestrator"
MAX_CLAUDE_ITERATIONS = 3
MAX_TASKS_PER_SESSION = 8
MILESTONE_INTENT_PATTERNS = (
    re.compile(r"\bready\s+to\s+(?:merge|ship|release|deploy)\b", re.IGNORECASE),
    re.compile(r"\b(?:ship|merge|land|deploy|release)\s+(?:it|this|now)\b", re.IGNORECASE),
    re.compile(r"\baaa\s+milestone\s+(?:release|ship|merge|deploy)\b", re.IGNORECASE),
)
DEFAULT_BASE_BRANCH = "main"
MIMO_WAIT_TIMEOUT_S = 70
AUTO_PR_SIDE_EFFECT_ATTEMPTS = 3
AUTO_PR_RETRY_DELAY_S = 2

CLAUDE_ITERATIONS_PATH = Path.home() / ".claude" / "state" / "claude_iterations.json"
AAA_PROTOCOL_PATH = Path.home() / ".claude" / "specs" / "aaa_protocol.md"
MIMO_VERDICT_PATH = Path.home() / ".claude" / "state" / "mimo_last_verdict.json"
HOOK_ENV_PATH = Path.home() / ".claude" / "secrets" / "hook_env"


def hook_env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    try:
        for line in HOOK_ENV_PATH.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            if key.strip() == name:
                return raw.strip().strip("\"'")
    except Exception:
        pass
    return default


AUTO_PR_ENABLED = hook_env("CODEX_HOOK_AUTO_PR", "1") != "0"
CLAUDE_AUDIT_ENABLED = hook_env("CODEX_HOOK_CLAUDE_AUDIT", "1") != "0"


def block(reason: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.stdout.flush()
    sys.exit(0)


def parse_verdict(text: str) -> tuple[str, str]:
    stripped = (text or "").strip()
    if stripped.startswith("VERDICT: PASS"):
        return "PASS", stripped
    if stripped.startswith("VERDICT: ISSUES"):
        return "ISSUES", stripped
    if stripped:
        return "ISSUES", "VERDICT: ISSUES\n1. Auditor returned nonstandard review text. Treat it as blocking feedback.\n\n" + stripped[:1800]
    return "ERROR", ""


def claude_iterations(session_id: str) -> int:
    return int(load_json_locked(CLAUDE_ITERATIONS_PATH, {}).get(session_id, 0))


def save_claude_iterations(session_id: str, value: int) -> None:
    def updater(data: dict) -> dict:
        data[session_id] = value
        return data

    update_json_locked(CLAUDE_ITERATIONS_PATH, updater)


def audit_with_claude(session_id: str, transcript: str, session_spec: str) -> bool:
    if not CLAUDE_AUDIT_ENABLED:
        log(COMPONENT, "claude audit disabled by CODEX_HOOK_CLAUDE_AUDIT=0")
        return True
    if len(transcript) < 400:
        log(COMPONENT, "claude audit skipped: transcript too short")
        return True
    prompt = f"""Audit Claude Code's latest completed task. First line must be VERDICT: PASS or VERDICT: ISSUES.

Focus on security, scope adherence, test coverage, async/concurrency correctness, and boundary error handling.
Do not nitpick style.

Original/session spec:
---
{session_spec[:2000]}
---

Current task transcript:
---
{transcript}
---
"""
    try:
        response = call_claude(prompt, system="You are a strict second-pass code auditor. Output only the verdict format.", max_tokens=1500)
    except Exception as exc:
        if not AUTO_PR_ENABLED:
            log(COMPONENT, f"claude audit skipped with Auto-PR disabled: {exc}")
            return True
        log(COMPONENT, f"claude audit fail-closed: {exc}")
        block("[Claude audit]\n\nClaude audit errored before commit/PR side effects. Fix API access or disable Auto-PR for this run.\n\n" + str(exc))
        return False
    if not response:
        if not AUTO_PR_ENABLED:
            log(COMPONENT, "claude audit skipped with Auto-PR disabled: missing key or empty response")
            return True
        log(COMPONENT, "claude audit fail-closed: missing key or empty response")
        block(
            "[Claude audit]\n\n"
            "Claude audit did not run before commit/PR side effects. Configure "
            "`ANTHROPIC_API_KEY` or `~/.claude/secrets/anthropic_api_key`, or set "
            "`CODEX_HOOK_AUTO_PR=0` if you want to allow audited-by-MiMo-only stops."
        )
        return False
    verdict, normalized = parse_verdict(response)
    log(COMPONENT, f"claude verdict: {normalized[:240]}")
    if verdict == "PASS":
        save_claude_iterations(session_id, 0)
        return True
    count = claude_iterations(session_id) + 1
    save_claude_iterations(session_id, count)
    if count >= MAX_CLAUDE_ITERATIONS:
        log(COMPONENT, f"claude max iterations reached ({count}); blocking side effects")
        block(f"[Claude audit — max iterations {count}/{MAX_CLAUDE_ITERATIONS}]\n\n{normalized}\n\nStopping side effects instead of shipping unapproved code.")
        return False
    block(f"[Claude audit — iteration {count}/{MAX_CLAUDE_ITERATIONS}]\n\n{normalized}\n\nFix these before stopping.")
    return False


def wait_for_mimo_pass(session_id: str, turn_id: str) -> bool:
    started_at = time.time()
    deadline = time.time() + MIMO_WAIT_TIMEOUT_S
    while time.time() < deadline:
        state = load_json_locked(MIMO_VERDICT_PATH, {}).get(session_id) or {}
        if state and (not turn_id or state.get("turn_id") == turn_id):
            updated_at = float(state.get("updated_at") or 0)
            if turn_id and updated_at < started_at:
                log(COMPONENT, f"mimo gate stale same-turn verdict ignored turn={turn_id}")
                time.sleep(1)
                continue
            age = time.time() - updated_at
            if not turn_id and age > 120:
                log(COMPONENT, f"mimo gate stale verdict ignored age={age:.1f}s")
                return False
            verdict = state.get("verdict")
            log(COMPONENT, f"mimo gate verdict={verdict} turn={state.get('turn_id', '')}")
            return verdict == "PASS"
        time.sleep(1)
    log(COMPONENT, "mimo gate timeout; side effects skipped")
    return False


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


def run_aaa_gate(cwd: Path, transcript: str) -> bool:
    if not milestone_detected(transcript):
        return True
    protocol = ""
    try:
        protocol = AAA_PROTOCOL_PATH.read_text()
    except Exception:
        log(COMPONENT, "AAA protocol cache missing; using lean built-in gate")
    checks: list[str] = []
    rc, out, err = safe_run(["git", "diff", "--name-only"], cwd=cwd)
    changed = [cwd / line for line in out.splitlines() if line.strip()] if rc == 0 else []
    quality = run_quality_checks(changed, cwd, timeout=120) if changed else {"ok": True, "results": []}
    if not quality.get("ok"):
        checks.append("Quality checks failed:\n" + "\n".join(
            f"- {item['tool']} {item['file']}: {item['output'][:1000]}"
            for item in quality.get("results", []) if not item.get("ok")
        ))
    if changed and not protocol:
        log(COMPONENT, "AAA gate ran with cached protocol missing")
    project_failures = run_project_aaa_checks(cwd, changed)
    checks.extend(project_failures)
    if checks:
        block("[AAA Milestone Gate]\n\n" + "\n\n".join(checks))
        return False
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


def retry_bool(label: str, fn) -> bool:
    for attempt in range(1, AUTO_PR_SIDE_EFFECT_ATTEMPTS + 1):
        if fn():
            if attempt > 1:
                log(COMPONENT, f"{label} recovered on attempt {attempt}")
            return True
        log(COMPONENT, f"{label} failed attempt {attempt}/{AUTO_PR_SIDE_EFFECT_ATTEMPTS}")
        if attempt < AUTO_PR_SIDE_EFFECT_ATTEMPTS:
            time.sleep(AUTO_PR_RETRY_DELAY_S)
    return False


def retry_text(label: str, fn) -> str:
    for attempt in range(1, AUTO_PR_SIDE_EFFECT_ATTEMPTS + 1):
        value = fn()
        if value:
            if attempt > 1:
                log(COMPONENT, f"{label} recovered on attempt {attempt}")
            return value
        log(COMPONENT, f"{label} failed attempt {attempt}/{AUTO_PR_SIDE_EFFECT_ATTEMPTS}")
        if attempt < AUTO_PR_SIDE_EFFECT_ATTEMPTS:
            time.sleep(AUTO_PR_RETRY_DELAY_S)
    return ""


def write_task_handoff(handoff: dict, session_id: str, task_n: int, summary: str, branch: str, sha: str, pr_url: str, status: str) -> None:
    handoff["session_id"] = session_id
    handoff["tasks_completed_count"] = task_n
    handoff["last_task"] = {
        "description": summary,
        "branch": branch,
        "pr_url": pr_url,
        "sha": sha,
        "status": status,
    }
    write_handoff(handoff)


def commit_push_pr(session_id: str, cwd: Path, transcript: str) -> tuple[str, str, str, str]:
    if not is_git_repo(cwd) or not has_worktree_changes(cwd):
        log(COMPONENT, "auto-pr skipped: no git repo or no changes")
        return "", "", "", "skipped"
    handoff = load_handoff()
    task_n = int(handoff.get("tasks_completed_count", 0)) + 1
    branch = task_branch(session_id, task_n)

    if not AUTO_PR_ENABLED:
        log(COMPONENT, f"auto-pr disabled by CODEX_HOOK_AUTO_PR=0; would create {branch}")
        return branch, "", "", "disabled"

    ok, reason = github_pr_preflight(cwd)
    if not ok:
        log(COMPONENT, f"auto-pr skipped before side effects: {reason}")
        return "", "", "", "preflight_failed"
    handoff["cwd"] = str(cwd)

    if not ensure_branch(current_session_branch(session_id), cwd):
        log(COMPONENT, "auto-pr skipped: could not ensure session branch")
        return "", "", "", "branch_failed"
    if not ensure_branch(branch, cwd):
        log(COMPONENT, f"auto-pr skipped: could not ensure task branch {branch}")
        return "", "", "", "branch_failed"

    summary = transcript.splitlines()[0][:80] if transcript.strip() else "Claude Code task"
    auditors = ["MiMo-v2.5-Pro"]
    if CLAUDE_AUDIT_ENABLED:
        auditors.append("Claude")
    message = f"[task {task_n}] {summary}\n\nAudited-by: {', '.join(auditors)}\nSession: {session_id}"
    ok, sha = commit(message, cwd)
    if not ok:
        log(COMPONENT, f"auto-pr commit skipped: {sha}")
        return branch, "", "", "commit_failed"
    pushed = retry_bool("auto-pr push", lambda: push(branch, cwd))
    if not pushed:
        write_task_handoff(handoff, session_id, task_n, summary, branch, sha, "", "push_failed")
        log(COMPONENT, f"auto-pr incomplete: push failed branch={branch} sha={sha}")
        return branch, sha, "", "push_failed"
    body = "\n\n".join([
        "Auto-generated Claude Code PR.",
        f"Session: {session_id}",
        "Files changed:",
        files_changed_summary(cwd) or "(see commit)",
        "Known limitations: generated by local hook pipeline; manual review required.",
    ])
    pr_url = retry_text(
        "auto-pr create",
        lambda: open_pr(f"[claude] task {task_n}: {summary}", body, base=DEFAULT_BASE_BRANCH, cwd=cwd),
    )
    if not pr_url:
        write_task_handoff(handoff, session_id, task_n, summary, branch, sha, "", "pr_failed")
        log(COMPONENT, f"auto-pr incomplete: PR creation failed branch={branch} sha={sha}")
        return branch, sha, "", "pr_failed"
    write_task_handoff(handoff, session_id, task_n, summary, branch, sha, pr_url, "created")
    log(COMPONENT, f"auto-pr branch={branch} sha={sha} pr={pr_url}")
    return branch, sha, pr_url, "created"


def next_task(session_id: str, cwd: Path, branch: str, pr_url: str) -> str:
    queued = pop_next_task("claude", cwd)
    if queued:
        title = queued.get("title") or "Queued task"
        body = queued.get("body") or ""
        source = queued.get("source_file") or queued.get("source") or "queue"
        return f"{title}\n\n{body}\n\nSource: {source}"

    handoff = load_handoff()
    handoff_cwd = handoff.get("cwd")
    if handoff_cwd and Path(handoff_cwd).resolve() != cwd.resolve():
        log(COMPONENT, f"self-spec skipped stale handoff cwd={handoff_cwd}")
        handoff = {}
    completed = int(handoff.get("tasks_completed_count", 0))
    if completed >= MAX_TASKS_PER_SESSION:
        log(COMPONENT, "self-spec skipped: max task budget")
        return ""
    backlog = handoff.get("queued_backlog") or []
    if backlog:
        item = backlog.pop(0)
        handoff["queued_backlog"] = backlog
        write_handoff(handoff)
        return f"{item.get('description')}\n\nAcceptance: update {item.get('source_file')} and run the relevant check."

    prompt = f"""Propose ONE next coding task for this repo.
It must be concrete, independently shippable, and doable in 1-4 tool calls.

Repo: {cwd}
Just shipped: branch={branch or 'none'} pr={pr_url or 'none'}
Recent git log:
{safe_run(['git', 'log', '--oneline', '-5'], cwd=cwd)[1] if is_git_repo(cwd) else ''}

Respond exactly:
NEXT_TASK_TITLE: <one line>
NEXT_TASK_BODY: <2-3 sentences with file paths and acceptance>

Or:
STATUS: COMPLETE
"""
    try:
        response = call_mimo(prompt, max_tokens=600)
    except Exception as exc:
        log(COMPONENT, f"self-spec fail-open: {exc}")
        return ""
    if not response or response.startswith("STATUS: COMPLETE"):
        log(COMPONENT, "self-spec complete/empty")
        return ""
    return response.strip()


def main() -> None:
    try:
        payload = normalize_payload(json.loads(sys.stdin.read() or "{}"))
        session_id = payload.get("session_id") or "unknown"
        turn_id = payload.get("turn_id") or ""
        cwd = Path(payload.get("cwd") or ".").resolve()
        transcript = latest_task_slice_claude(payload.get("transcript_path") or "", payload.get("last_assistant_message") or "")
        cwd = resolve_git_cwd(cwd, transcript=transcript)
        remember_repo(cwd)
        session_spec = read_session_spec(session_id)

        log(COMPONENT, f"start transcript={len(transcript)} cwd={cwd}")
        if not audit_with_claude(session_id, transcript, session_spec):
            return
        if is_git_repo(cwd) and not run_aaa_gate(cwd, transcript):
            return
        if not wait_for_mimo_pass(session_id, turn_id):
            log(COMPONENT, "side effects skipped: MiMo did not pass current turn")
            return
        branch, sha, pr_url, auto_pr_status = commit_push_pr(session_id, cwd, transcript)
        if auto_pr_status in {"push_failed", "pr_failed"}:
            block(
                "[Auto-PR incomplete]\n\n"
                f"Status: {auto_pr_status}\n"
                f"Branch: {branch}\n"
                f"Commit: {sha}\n\n"
                "Do not start the next task yet. Recover this unit first by retrying "
                "`git push -u origin <branch>` and/or `gh pr create` for the branch above."
            )
        if pr_url:
            task = next_task(session_id, cwd, branch, pr_url)
            if task:
                block(
                    "[Self-Spec Generator] Continuing autonomous run.\n\n"
                    f"Just shipped: {pr_url or branch}\n\n"
                    f"NEXT TASK:\n{task}\n\nBegin work. The audit/checkpoint pipeline will run again when complete."
                )
        log(COMPONENT, "allow stop")
    except Exception as exc:
        log(COMPONENT, f"fail-open: {exc}")


if __name__ == "__main__":
    main()

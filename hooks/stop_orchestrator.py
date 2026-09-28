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

from lib.api import call_claude
from lib.autonomy import remember_repo
from lib.git import (
    is_git_repo,
    resolve_git_cwd,
    safe_run,
)
from lib.json_state import load_json_locked, update_json_locked
from lib.log import log
from lib.quality import pending_quality, run_project_gate, run_quality_checks
from lib.state import read_session_spec
from lib.transcript import latest_task_entries, latest_task_slice


COMPONENT = "stop_orchestrator"
MAX_CLAUDE_ITERATIONS = 3
MILESTONE_INTENT_PATTERNS = (
    re.compile(r"\bready\s+to\s+(?:merge|ship|release|deploy)\b", re.IGNORECASE),
    re.compile(r"\b(?:ship|merge|land|deploy|release)\s+(?:it|this|now)\b", re.IGNORECASE),
    re.compile(r"\baaa\s+milestone\s+(?:release|ship|merge|deploy)\b", re.IGNORECASE),
)
PAUSE_REQUEST_RE = re.compile(
    r"^USER:\s*(?:ok[\s,]+)?(?:stop|pause|stand\s*by|hold off)\s+(?:for now|until\b)",
    re.IGNORECASE,
)

CLAUDE_ITERATIONS_PATH = Path.home() / ".codex" / "state" / "claude_iterations.json"
AAA_PROTOCOL_PATH = Path.home() / ".codex" / "specs" / "aaa_protocol.md"
HOOK_ENV_PATH = Path.home() / ".codex" / "secrets" / "hook_env"


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
    prompt = f"""Audit Codex's latest completed task. First line must be VERDICT: PASS or VERDICT: ISSUES.

Focus on security, scope adherence, test coverage, Semble discovery plus Codegraph structural/impact routing, async/concurrency correctness, and boundary error handling.
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
            "`ANTHROPIC_API_KEY` or `~/.codex/secrets/anthropic_api_key`, or set "
            "`CODEX_HOOK_AUTO_PR=0` if you want to allow GPT-audited stops."
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


def run_aaa_gate(cwd: Path, transcript: str, timeout: float = 420) -> bool:
    if (cwd / ".quality-gate.json").exists():
        result = run_project_gate(cwd, timeout=timeout)
        log(COMPONENT, f"repository quality gate ok={result['ok']}")
        if not result["ok"]:
            block("[Repository Quality Gate]\n\n" + "\n".join(
                f"{item['tool']}: {item['output']}" for item in result["results"] if not item["ok"]
            ))
            return False
        return True
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


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = payload.get("session_id") or "unknown"
        turn_id = payload.get("turn_id") or ""
        cwd = Path(payload.get("cwd") or ".").resolve()
        transcript = latest_task_slice(payload.get("transcript_path") or "", payload.get("last_assistant_message") or "")
        cwd = resolve_git_cwd(cwd, transcript=transcript)
        remember_repo(cwd)
        session_spec = read_session_spec(session_id)

        log(COMPONENT, f"start session={session_id} turn={turn_id} transcript={len(transcript)} cwd={cwd}")
        if PAUSE_REQUEST_RE.search(transcript) and not any(
                role == "TOOL CALL" for role, _ in latest_task_entries(payload.get("transcript_path") or "")):
            log(COMPONENT, "explicit pause: pending quality retained, checks deferred")
            return
        pending = pending_quality(session_id)
        quality_deadline = time.monotonic() + 480
        for project in pending:
            target = Path(project)
            if not target.exists():
                log(COMPONENT, f"discarding deleted pending-quality repo: {target}")
                continue
            if not (target / ".quality-gate.json").exists():
                block(f"[Repository Quality Gate] Missing {target / '.quality-gate.json'}; declare the repository quality commands before completing code changes.")
            if not run_aaa_gate(target, transcript, timeout=quality_deadline - time.monotonic()):
                return
        if (str(cwd) not in pending and is_git_repo(cwd) and milestone_detected(transcript)
                and not run_aaa_gate(cwd, transcript, timeout=quality_deadline - time.monotonic())):
            return
        if pending:
            pending_quality(session_id, clear=True)
        if not audit_with_claude(session_id, transcript, session_spec):
            return
        log(COMPONENT, "allow stop")
    except Exception as exc:
        log(COMPONENT, f"gate error: {exc}")
        block(f"[Repository Quality Gate] Unable to verify completion: {exc}")


if __name__ == "__main__":
    main()

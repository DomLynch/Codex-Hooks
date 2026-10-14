from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .autonomy import hook_env
from .git import safe_run
from .log import log


CLAUDE_SECRETS_DIR = Path.home() / ".claude" / "secrets"
CODEX_SECRETS_DIR = Path.home() / ".codex" / "secrets"  # Fallback for shared audit keys
CODEX_AUDIT_MODEL = os.environ.get("CODEX_AUDIT_MODEL", "gpt-5.6-terra")
CODEX_AUDIT_EFFORT = os.environ.get("CODEX_AUDIT_EFFORT", "high")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
# Reviewer model and effort: the environment, then ~/.claude/secrets/hook_env (where the owner sets them), then the default.
CLAUDE_AUDIT_MODEL = hook_env("CLAUDE_AUDIT_MODEL", "claude-sonnet-5").strip() or "claude-sonnet-5"
CLAUDE_AUDIT_EFFORT = hook_env("CLAUDE_AUDIT_EFFORT", "high").strip().lower() or "high"
# "claude" = Claude CLI on the user's subscription; "codex" = GPT via Codex CLI.
AUDIT_TRANSPORT = hook_env("CLAUDE_AUDIT_TRANSPORT", "claude").strip().lower()
# Set on the audit child's environment so hooks inside it exit immediately (no recursion).
AUDIT_CHILD_ENV = "CLAUDE_HOOK_AUDIT_CHILD"
AUDITOR_NAME = f"Claude {CLAUDE_AUDIT_MODEL}" if AUDIT_TRANSPORT == "claude" else CODEX_AUDIT_MODEL


def exit_if_audit_child() -> None:
    """Hooks call this first: a Claude audit child must never run hooks, or it recurses."""
    if os.environ.get(AUDIT_CHILD_ENV):
        raise SystemExit(0)


def anthropic_text(data: dict) -> str:
    parts = data.get("content") or []
    return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()


def load_api_key(env_name: str, secrets_filename: str) -> str:
    """Load an API key. Order: env var → ~/.claude/secrets → ~/.codex/secrets → shell rc parse.
    The ~/.codex fallback lets Anthropic keys be shared across both hook systems.
    """
    key = os.environ.get(env_name)
    if key:
        return key.strip()
    for base in (CLAUDE_SECRETS_DIR, CODEX_SECRETS_DIR):
        path = base / secrets_filename
        if path.exists():
            try:
                return path.read_text().strip()
            except Exception:
                pass
    for rc_file in (".zshrc", ".zprofile", ".profile"):
        p = Path.home() / rc_file
        if not p.exists():
            continue
        try:
            lines = p.read_text(errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("export "):
                stripped = stripped[len("export "):].strip()
            if not stripped.startswith(f"{env_name}="):
                continue
            raw = stripped.split("=", 1)[1].strip()
            try:
                parts = shlex.split(raw, comments=False, posix=True)
            except ValueError:
                parts = [raw.strip("\"'")]
            return parts[0] if parts else ""
    return ""


def codex_binary() -> str:
    candidates = (
        os.environ.get("CODEX_AUDIT_BIN"),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        shutil.which("codex"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Codex CLI is unavailable; set CODEX_AUDIT_BIN")


def claude_binary() -> str:
    # GUI-launched hooks don't inherit the shell PATH; list the usual absolute paths.
    candidates = (
        os.environ.get("CLAUDE_AUDIT_BIN"),
        shutil.which("claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        str(Path.home() / ".local" / "bin" / "claude"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Claude CLI is unavailable; set CLAUDE_AUDIT_BIN")


# One judge at a time, and none on a saturated Mac (Strategy + Lead, Dom's authority, 2026-10-14 06:50 Dubai: Mac load 15-22 on 10 cores
# all night while every lane's Stop spawned its own `claude -p`). The lock is a flock on a state file: the kernel drops it if the holder
# dies, so it can never go stale. A skipped judge raises JudgeBusy; the Stop hook releases the turn unreviewed rather than blocking it,
# because a block makes the lane retry, which is one more judge on the machine that is already too busy.
JUDGE_LOCK_PATH = Path.home() / ".claude" / "state" / "judge.lock"


def _max_load() -> float:
    try:
        return float(hook_env("CLAUDE_JUDGE_MAX_LOAD", "10"))
    except ValueError:
        return 10.0


JUDGE_MAX_LOAD = _max_load()


class JudgeBusy(RuntimeError):
    """The judge was not started: the machine is over the load line or another judge is running."""


def judge_load1() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0   # no load figure: never block the reviewer on a missing measurement


@contextmanager
def judge_slot(lane: str):
    load = judge_load1()
    if load > JUDGE_MAX_LOAD:
        log("claude_audit", f"JUDGE_SKIPPED lane={lane} reason=load load1={load:.1f} max={JUDGE_MAX_LOAD:g}")
        raise JudgeBusy(f"machine busy: load1={load:.1f} is over {JUDGE_MAX_LOAD:g}")
    JUDGE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(JUDGE_LOCK_PATH, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        log("claude_audit", f"JUDGE_SKIPPED lane={lane} reason=lock another judge is running load1={load:.1f}")
        raise JudgeBusy("another judge is already running on this machine") from None
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def call_claude_cli(
    prompt: str,
    max_tokens: int = 1500,
    timeout: int = 45,
    lane: str = "shared",
    system: str = "",
) -> str:
    """Audit via the Claude CLI on the user's subscription (no API key).

    The child inherits AUDIT_CHILD_ENV so every hook it would fire exits at once.
    Mutating tools are disallowed: the auditor only reads the prompt and replies.
    """
    del max_tokens
    instruction = f"{system}\n\n{prompt}" if system else prompt
    started = time.monotonic()
    env = {**os.environ, AUDIT_CHILD_ENV: "1"}
    # `claude setup-token` prints a long-lived token; it lives in hook_env, not the keychain.
    token = hook_env("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    # The review prompt is self-contained; user plugins, MCP and saved sessions add startup work.
    command = [
        claude_binary(), "-p",
        "--safe-mode", "--no-session-persistence",
        "--model", CLAUDE_AUDIT_MODEL,
        "--effort", CLAUDE_AUDIT_EFFORT,
        "--disallowedTools", "Bash Edit Write NotebookEdit",
    ]
    with judge_slot(lane), tempfile.TemporaryDirectory(prefix="claude-audit-") as workdir:
        try:
            returncode, stdout, stderr = safe_run(command, cwd=workdir, timeout=timeout, env=env, input_text=instruction, raise_on_timeout=True)
        except TimeoutError as exc:
            raise TimeoutError(f"Claude audit timed out after {timeout}s") from exc
    if returncode:
        raise RuntimeError(f"Claude audit failed ({returncode}): {(stderr or stdout).strip()[-500:]}")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log("claude_audit", f"CLAUDE_AUDIT lane={lane} model={CLAUDE_AUDIT_MODEL} effort={CLAUDE_AUDIT_EFFORT} elapsed_ms={elapsed_ms}")
    return stdout.strip()


def call_mimo(
    prompt: str,
    max_tokens: int = 1500,
    timeout: int = 45,
    lane: str = "shared",
    system: str = "",
    api_key: str = "",
) -> str:
    if AUDIT_TRANSPORT == "claude":
        return call_claude_cli(prompt, max_tokens=max_tokens, timeout=timeout, lane=lane, system=system)
    del api_key, max_tokens
    instruction = f"{system}\n\n{prompt}" if system else prompt
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codex-audit-") as workdir:
        output_path = Path(workdir) / "verdict.txt"
        command = [
            codex_binary(), "exec", "--ignore-user-config", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only",
            "--model", CODEX_AUDIT_MODEL,
            "--config", f'model_reasoning_effort="{CODEX_AUDIT_EFFORT}"',
            "--output-last-message", str(output_path), "-",
        ]
        try:
            returncode, stdout, stderr = safe_run(command, cwd=workdir, timeout=timeout, input_text=instruction, raise_on_timeout=True)
        except TimeoutError as exc:
            raise TimeoutError(f"Codex audit timed out after {timeout}s") from exc
        if returncode:
            raise RuntimeError(f"Codex audit failed ({returncode}): {(stderr or stdout).strip()[-500:]}")
        response = output_path.read_text(errors="replace").strip() if output_path.exists() else stdout.strip()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log("codex_audit", f"CODEX_AUDIT lane={lane} model={CODEX_AUDIT_MODEL} effort={CODEX_AUDIT_EFFORT} elapsed_ms={elapsed_ms}")
    return response

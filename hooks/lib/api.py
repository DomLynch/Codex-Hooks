from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .log import log


CODEX_AUDIT_MODEL = os.environ.get("CODEX_AUDIT_MODEL", "gpt-6-sol")
CODEX_AUDIT_EFFORT = os.environ.get("CODEX_AUDIT_EFFORT", "low")


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


def call_mimo(
    prompt: str,
    max_tokens: int = 1500,
    timeout: int = 45,
    lane: str = "shared",
    system: str = "",
    api_key: str = "",
) -> str:
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
            completed = subprocess.run(
                command, input=instruction, text=True, capture_output=True,
                cwd=workdir, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Codex audit timed out after {timeout}s") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise RuntimeError(f"Codex audit failed ({completed.returncode}): {detail}")
        response = output_path.read_text(errors="replace").strip() if output_path.exists() else completed.stdout.strip()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log("codex_audit", f"CODEX_AUDIT lane={lane} model={CODEX_AUDIT_MODEL} effort={CODEX_AUDIT_EFFORT} elapsed_ms={elapsed_ms}")
    return response

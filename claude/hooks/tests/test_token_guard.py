"""token_guard: a Hugging Face token must never reach a command line, commit, PR body or message (Dom via Strategy, 2026-10-21).
Dom put an account-wide token on the VPS at /root/.hf/token. Only a job script reads it (HF_TOKEN=$(cat /root/.hf/token) inside the script);
a Bash command line may not print, copy or substitute it, and no command may carry a token-shaped string (hf_ + 34 letters/digits)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import token_guard as g  # noqa: E402

FAKE = "hf_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"[:34]   # token-shaped, not a real token

DENY = [
    f"git commit -m 'add token {FAKE}'",
    f'gh pr create --title x --body "key {FAKE}"',
    f"echo {FAKE}",
    f"curl -H 'Authorization: Bearer {FAKE}' https://huggingface.co/api/whoami-v2",
    "cat /root/.hf/token",
    "cat ~/.hf/token",
    "cat < /root/.hf/token",
    "head -c 8 /root/.hf/token",
    "base64 /root/.hf/token",
    "cp /root/.hf/token /tmp/t",
    "scp root@49.12.7.18:/root/.hf/token .",
    "echo $(cat /root/.hf/token)",
    "HF_TOKEN=$(cat /root/.hf/token) hf upload a b",
    "export HF_TOKEN=`cat /root/.hf/token`",
    "ssh root@49.12.7.18 'cat /root/.hf/token'",
    "ssh -i k root@49.12.7.18 \"cat /root/.hf/token | head -c 4\"",
    "bash -c 'cat /root/.hf/token'",
    "cd /root && cat .hf/token",
]
ALLOW = [
    "ls -l /root/.hf/token",
    "stat /root/.hf/token",
    "test -r /root/.hf/token && echo present",
    "[ -r /root/.hf/token ] && echo present",
    "ssh root@49.12.7.18 'ls -l /root/.hf/token'",
    "bash /root/jobs/upload.sh",
    "ssh root@49.12.7.18 'bash /root/jobs/upload.sh'",
    "git commit -m 'HF jobs read the token from the file inside the script, see /root/.hf/token docs'",
    "git commit -m 'hf_hub_download works'",
    "echo hf_abc123",
    "pip install huggingface_hub",
    "hf upload Domlynch/oh-re-work out/logs.zst",
    "ls ~/.hf",
    "",
]


def test_denies_token_shape_and_token_file_reads():
    missed = [c for c in DENY if not g.hf_token_reason(c)]
    assert not missed, missed


def test_allows_everything_else():
    wrongly_blocked = [c for c in ALLOW if g.hf_token_reason(c)]
    assert not wrongly_blocked, wrongly_blocked


def test_reason_never_repeats_the_token():
    reason = g.hf_token_reason(f"echo {FAKE}")
    assert reason and FAKE not in reason


def _entrypoint(command: str) -> str:
    """Stdout of the real deploy_guard.py hook for one Bash command (a deny prints JSON, an allow prints nothing)."""
    import json
    import subprocess

    guard = Path(__file__).resolve().parents[1] / "deploy_guard.py"
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}
    return subprocess.run([sys.executable, str(guard)], input=json.dumps(payload), capture_output=True, text=True).stdout


def test_deploy_guard_entrypoint_denies_and_never_echoes_the_token():
    out = _entrypoint(f"git commit -m 'x {FAKE}'")
    assert '"permissionDecision": "deny"' in out and FAKE not in out
    assert '"permissionDecision": "deny"' in _entrypoint("cat /root/.hf/token")
    assert _entrypoint("ls -l /root/.hf/token") == ""


def test_shorter_or_longer_lookalikes():
    assert not g.hf_token_reason("echo hf_" + "a" * 20)      # too short to be a token
    assert g.hf_token_reason("echo hf_" + "a" * 40)          # a longer string still contains a full token shape

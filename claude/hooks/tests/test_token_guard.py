"""token_guard: a Hugging Face token must never reach a command line, commit, PR body or message (Dom via Strategy, 2026-10-21).
Dom put an account-wide token on the VPS at /root/.hf/token. Only a job script reads it (HF_TOKEN=$(cat /root/.hf/token) inside the script).

Auditor recheck, 2026-10-21: the first version matched the WORDING (`cat /root/.hf/token`) and let these through: globs, a quote-split path,
a path built in a variable or with cd, python -c open(), tar/scp of the folder, echo $HF_TOKEN. The rule is: fail CLOSED on any command whose
resolved arguments touch /root/.hf (or ~/.hf), except ls/stat/test and the text of a message flag; a token split across strings cannot be
caught by a regex, so the guard closes the read."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import token_guard as g  # noqa: E402

FAKE = "hf_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"[:34]   # token-shaped, not a real token

DENY = [
    # the shape of the token itself
    f"git commit -m 'add token {FAKE}'",
    f'gh pr create --title x --body "key {FAKE}"',
    f"echo {FAKE}",
    f"curl -H 'Authorization: Bearer {FAKE}' https://huggingface.co/api/whoami-v2",
    # the Auditor's variants
    "cat /root/.hf/token",
    "cat /root/.hf/tok*",
    "cat /root/.hf/*",
    "cat /root/.hf/to''ken",
    'cat "/root/.hf/to"ken',
    "F=/root/.hf; cat $F/token",
    "F=/root/.hf && cat ${F}/token",
    "cd /root/.hf && cat token",
    "cd /root && cat .hf/token",
    "python3 -c \"print(open('/root/.hf/token').read())\"",
    "python3 -c \"import pathlib; print(pathlib.Path.home().joinpath('.hf/token').read_text())\"",
    "tar czf /tmp/x.tgz /root/.hf",
    "scp -r /root/.hf root@49.12.7.18:/tmp/",
    "scp root@49.12.7.18:/root/.hf/token .",
    "echo $HF_TOKEN",
    'curl -H "Authorization: Bearer $HF_TOKEN" https://huggingface.co/api/whoami-v2',
    "printenv HF_TOKEN",
    # globs that name the folder, and copiers of a parent
    "cat /root/.h*/token",
    "cat /root/.*/token",
    "tar czf /tmp/root.tgz /root",
    "rsync -a /root/ /tmp/rootcopy/",
    # the plain reads
    "cat ~/.hf/token",
    "cat < /root/.hf/token",
    "head -c 8 /root/.hf/token",
    "base64 /root/.hf/token",
    "cp /root/.hf/token /tmp/t",
    "echo $(cat /root/.hf/token)",
    "HF_TOKEN=$(cat /root/.hf/token) hf upload a b",
    "export HF_TOKEN=`cat /root/.hf/token`",
    "ssh root@49.12.7.18 'cat /root/.hf/token'",
    "ssh -i k root@49.12.7.18 \"cat /root/.hf/token | head -c 4\"",
    "ssh root@49.12.7.18 'F=/root/.hf; cat $F/token'",
    "bash -c 'cat /root/.hf/token'",
    "gh pr create --body-file /root/.hf/token",
]
ALLOW = [
    "ls -l /root/.hf/token",
    "ls ~/.hf",
    "stat /root/.hf/token",
    "test -r /root/.hf/token && echo present",
    "[ -r /root/.hf/token ] && echo present",
    'test -n "$HF_TOKEN"',
    "ssh root@49.12.7.18 'ls -l /root/.hf/token'",
    "bash /root/jobs/upload.sh",
    "ssh root@49.12.7.18 'bash /root/jobs/upload.sh'",
    "git commit -m 'HF jobs read the token from the file inside the script, see /root/.hf/token docs'",
    "git commit -m 'uses $HF_TOKEN inside the job script'",
    'gh pr create --title "token guard" --body "blocks cat /root/.hf/token and echo $HF_TOKEN"',
    "git commit -m 'hf_hub_download works'",
    "echo hf_abc123",
    "pip install huggingface_hub",
    "hf upload Domlynch/oh-re-work out/logs.zst",
    "cat /root/notes.txt",
    "cp /root/* /tmp/",          # a shell * does not match dot-folders
    "tar czf /tmp/w.tgz /root/work",
    "cd /root/work && cat token.txt",
    "python3 -c \"print(1+1)\"",
    "",
]


def test_denies_every_way_to_read_or_print_the_token():
    missed = [c for c in DENY if not g.hf_token_reason(c, "/tmp")]
    assert not missed, missed


def test_allows_listing_testing_and_message_text():
    wrongly_blocked = [c for c in ALLOW if g.hf_token_reason(c, "/tmp")]
    assert not wrongly_blocked, wrongly_blocked


def test_reason_never_repeats_the_token():
    reason = g.hf_token_reason(f"echo {FAKE}")
    assert reason and FAKE not in reason


def _entrypoint(command: str) -> str:
    """Stdout of the real deploy_guard.py hook for one Bash command (a deny prints JSON, an allow prints nothing)."""
    import json
    import subprocess

    guard = Path(__file__).resolve().parents[1] / "deploy_guard.py"
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/root/.hf"}
    return subprocess.run([sys.executable, str(guard)], input=json.dumps(payload), capture_output=True, text=True).stdout


def test_deploy_guard_entrypoint_denies_and_never_echoes_the_token():
    out = _entrypoint(f"git commit -m 'x {FAKE}'")
    assert '"permissionDecision": "deny"' in out and FAKE not in out
    assert '"permissionDecision": "deny"' in _entrypoint("cat /root/.hf/token")
    assert '"permissionDecision": "deny"' in _entrypoint("cat token")   # the hook's own cwd is the token folder
    assert _entrypoint("ls -l /root/.hf/token") == ""


def test_shorter_or_longer_lookalikes():
    assert not g.hf_token_reason("echo hf_" + "a" * 20)      # too short to be a token
    assert g.hf_token_reason("echo hf_" + "a" * 40)          # a longer string still contains a full token shape

"""Physics Dev + Dom, 2026-10-29: on 2026-10-28 an agent ran a `sed` meant to mask secrets over /root/.git-credentials on the VPS and printed a ghp_ token into the session transcript.
Extension of the standing never-echo rule (HF token, Dom 2026-10-21) to GitHub tokens and the other credential files: a Bash command that reads a credential file with a reader,
that carries a token-shaped string, or that dumps the environment is refused. Reads that only test existence (ls, stat, test, wc, rm) pass. The reason never repeats a secret."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard  # noqa: E402
from lib import credential_guard as g  # noqa: E402

FAKE = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"   # 36 chars after the prefix, not a real token
REFUSED = [
    "ssh root@49.12.7.18 \"sed -E 's/:[^@]*@/:***@/' /root/.git-credentials\"",
    "cat /root/.git-credentials",
    "cat ~/.git-credentials",
    "ssh root@vps 'grep github /root/.git-credentials'",
    "head -1 /root/.git-credentials | cut -d: -f2",
    "awk -F: '{print $2}' /root/.git-credentials",
    "sed -n 1p ~/.netrc",
    "cat ~/.netrc",
    "less ~/.config/gh/hosts.yml",
    "cat ~/.aws/credentials",
    "cat ~/.ssh/id_ed25519",
    "base64 /root/.git-credentials",
    "cp /root/.git-credentials /tmp/x",
    "scp root@vps:/root/.git-credentials /tmp/x",
    "python3 -c \"print(open('/root/.git-credentials').read())\"",
    "while read l; do echo $l; done < /root/.git-credentials",
    f"git clone https://{FAKE}@github.com/a/b.git",
    f"curl -H 'Authorization: token {FAKE}' https://api.github.com/user",
    "env",
    "printenv",
    "env | sort",
    "export -p",
    "declare -p",
    "ssh root@vps 'env | grep -i key'",
    "printenv GITHUB_TOKEN",
    "echo $GH_TOKEN",
    "echo $GITHUB_TOKEN",
]
ALLOWED = [
    "ls -l /root/.git-credentials",
    "stat /root/.git-credentials",
    "test -s /root/.git-credentials && echo has-creds",
    "wc -l /root/.git-credentials",
    "ssh root@vps 'wc -l < /root/.git-credentials; ls -l /root/.git-credentials'",
    "rm -f /root/.git-credentials",
    "git config --global credential.helper store",
    "git credential-store --file /root/.git-credentials erase",
    "gh auth status",
    "env FOO=1 npm --version",
    "env -i PATH=/usr/bin ls",
    "printenv HOME",
    "echo hello",
    "git commit -m 'never cat /root/.git-credentials'",
    "echo 'cat ~/.netrc is refused'",
    "cat ~/.ssh/id_ed25519.pub",
    "ls ~/.ssh",
    "git push origin main",
    "ssh root@vps 'df -h /'",
    "ssh-keygen -lf ~/.ssh/id_ed25519.pub",
]


def test_credential_reads_token_strings_and_env_dumps_are_refused_without_repeating_a_secret():
    for cmd in REFUSED:
        why = g.credential_reason(cmd)
        assert why, cmd
        assert "ghp_" not in why and FAKE not in why
        assert "2026-10-29" in why


def test_existence_checks_message_text_and_safe_forms_pass():
    for cmd in ALLOWED:
        assert g.credential_reason(cmd) == "", cmd


def test_empty_command_passes():
    assert g.credential_reason("") == ""


def run_main(command, mac=True):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/Users/x/Developer/physics"}
    out = io.StringIO()
    with mock.patch.object(deploy_guard, "_is_mac", return_value=mac), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
        deploy_guard.main()
    return out.getvalue()


def test_wired_into_the_pretooluse_deny_on_the_mac_and_off_it():
    for mac in (True, False):
        denied = json.loads(run_main(REFUSED[0], mac=mac))["hookSpecificOutput"]
        assert denied["permissionDecision"] == "deny"
        assert "credential" in denied["permissionDecisionReason"].lower()
        assert run_main(ALLOWED[0], mac=mac) == ""

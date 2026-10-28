"""Dom + Strategy, 2026-10-28: the VPS root disk hit 98% twice in a day and /root alone was 70 GB of per-task clones (f3v-*, bw-*, boss-*, v3-*, arma-*), which
vps-scratch-sweep cannot sweep (/root is not one of its roots). Strategy's ruling: refuse `git clone` and `git worktree add` into /root and point agents to
/srv/dev-jobs/<lane>/<name>, where the sweep archives them. The guard only refuses; it never deletes anything."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard  # noqa: E402
from lib import root_clone_guard as g  # noqa: E402

REFUSED = [
    "ssh root@49.12.7.18 'git clone https://github.com/DomLynch/Frankendom-v3.git /root/f3v-load'",
    "ssh -i k root@vps \"cd /root && git clone https://github.com/x/y.git\"",
    "ssh root@vps 'cd /root; git clone --depth 1 https://github.com/x/y bw-new'",
    "ssh root@vps 'cd /root/ && git clone https://github.com/x/y'",
    "ssh root@vps 'git -C /root/arma-chain worktree add /root/arma-chain-wt1 origin/main'",
    "ssh root@vps 'git worktree add /root/boss-x -b feat/x'",
    "git clone https://github.com/x/y.git /root/work-new",
    "ssh root@vps 'git clone -b main https://github.com/x/y.git /root/pd-rev9 && cd /root/pd-rev9 && npm ci'",
]
ALLOWED = [
    "ssh root@vps 'git clone https://github.com/x/y.git /srv/dev-jobs/v3/pickup'",
    "ssh root@vps 'cd /srv/dev-jobs/art-x && git clone https://github.com/x/y.git'",
    "ssh root@vps 'git -C /root/arma-chain pull --ff-only'",
    "ssh root@vps 'ls /root; du -sm /root/*'",
    "ssh root@vps 'cat /root/notes.md'",
    "git clone https://github.com/x/y.git ~/Developer/new",
    "rsync -a ./out/ root@vps:/root/stills/",
    "echo 'git clone x /root/y is refused'",
    "ssh root@vps 'git clone https://github.com/x/y.git /mnt/HC_Volume_107195252/dev-jobs/y'",
]


def test_clone_or_worktree_add_into_root_is_refused_with_the_new_place():
    for cmd in REFUSED:
        why = g.root_clone_reason(cmd)
        assert why, cmd
        assert "/srv/dev-jobs/<lane>/" in why
        assert "2026-10-28" in why


def test_everything_else_passes():
    for cmd in ALLOWED:
        assert g.root_clone_reason(cmd) == "", cmd


def test_empty_or_unrelated_commands_pass():
    assert g.root_clone_reason("") == ""
    assert g.root_clone_reason("git status") == ""
    assert g.root_clone_reason("ls /root") == ""


def run_main(command, mac=True):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/Users/x/Developer/frankendom-v3"}
    out = io.StringIO()
    with mock.patch.object(deploy_guard, "_is_mac", return_value=mac), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
        deploy_guard.main()
    return out.getvalue()


def test_wired_into_the_pretooluse_deny_on_the_mac():
    denied = json.loads(run_main(REFUSED[0]))["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny"
    assert "/srv/dev-jobs/<lane>/" in denied["permissionDecisionReason"]
    assert run_main(ALLOWED[0]) == ""

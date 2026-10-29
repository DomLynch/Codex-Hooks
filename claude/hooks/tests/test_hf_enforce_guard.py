"""Dom + Strategy, 2026-10-29: the VPS hit load 68 from lane suites and builds; Dom's ruling is "all lanes' suites/builds move to Hugging Face CPU, VPS = live sites only".
Lanes kept forgetting hfjob because nothing enforced it. The Mac PreToolUse hook now refuses any `ssh ... <heavy command>` (and the Mac-local heavy refusal says hfjob) with
"use hfjob -- <cmd>". Allow-list: deploys, the sweep tool, hfjob itself. It only refuses; it never runs or deletes anything."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard  # noqa: E402
from lib import hf_enforce_guard as g  # noqa: E402

REFUSED = [
    "ssh root@49.12.7.18 'cd /srv/dev-jobs/v3/x && npm test'",
    "ssh -i ~/.ssh/binance_futures_tool root@49.12.7.18 'cd /srv/dev-jobs/v3/x && npm run test:all'",
    "ssh root@vps \"cd /srv/dev-jobs/a && npx playwright test\"",
    "ssh root@vps 'cd /srv/dev-jobs/a && npm run build'",
    "ssh root@vps 'vite build'",
    "ssh root@vps 'blender -b x.blend -P bake.py'",
    "ssh root@vps 'node --test tests/*.test.ts'",
    "ssh root@vps 'capture --prio 3 web npm run test:all'",
    "ssh root@vps 'node scripts/release-checks.mjs'",
    "ssh root@vps 'node scripts/x-browser-check.mjs --opponent goblin'",
    "ssh -o BatchMode=yes root@100.96.74.1 'cd /srv/dev-jobs/a; npm ci && npm run quality'",
]
ALLOWED = [
    "ssh root@vps 'bash scripts/deploy.sh'",
    "ssh root@vps 'scripts/deploy.sh --rev abc'",
    "ssh root@vps 'vps-scratch-sweep --path=/root/x --apply'",
    "ssh root@vps 'hfjob -- npm test'",
    "ssh root@vps 'df -h /; uptime; ls /srv/dev-jobs'",
    "ssh root@vps 'git -C /srv/dev-jobs/v3/x pull --ff-only'",
    "ssh root@vps 'systemctl status nginx'",
    "ssh root@vps 'tail -50 /var/log/nginx/error.log'",
    "hfjob -- npm test",
    "git commit -m 'ssh root@vps npm test is refused'",
    "echo 'ssh root@vps npm test'",
    "npm test",
    "ssh root@vps 'echo npm test is heavy'",
]


def test_ssh_with_a_heavy_command_is_refused_and_points_to_hfjob():
    for cmd in REFUSED:
        why = g.ssh_heavy_reason(cmd)
        assert why, cmd
        assert "hfjob -- <cmd>" in why
        assert "2026-10-29" in why


def test_allow_list_and_everything_else_passes():
    for cmd in ALLOWED:
        assert g.ssh_heavy_reason(cmd) == "", cmd


def test_empty_command_passes():
    assert g.ssh_heavy_reason("") == ""


def run_main(command, mac=True):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/Users/x/Developer/frankendom-v3"}
    out = io.StringIO()
    with mock.patch.object(deploy_guard, "_is_mac", return_value=mac), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
        deploy_guard.main()
    return out.getvalue()


def test_wired_into_the_pretooluse_deny_on_the_mac():
    denied = json.loads(run_main(REFUSED[0]))["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny"
    assert "hfjob -- <cmd>" in denied["permissionDecisionReason"]
    assert run_main(ALLOWED[0]) == ""


def test_mac_local_heavy_refusal_now_names_hfjob():
    reason = deploy_guard.decide("npm test", None)
    assert "hfjob -- <cmd>" in reason

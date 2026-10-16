"""cache_guard: rm/mv/trash of the Playwright browser cache, or of a folder that contains it, is denied (Lead + Dom, 2026-10-16).
A cleanup command deleted ~/Library/Caches/ms-playwright and stopped the HL2 publish (chromium executable missing)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as g  # noqa: E402

HOME = str(Path.home())

DENY = [
    "rm -rf ~/Library/Caches/ms-playwright",
    f"rm -rf {HOME}/Library/Caches/ms-playwright/chromium-1234",
    "rm -rf ~/Library/Caches",
    "rm -rf ~/Library/Caches/*",
    "rm -rf $HOME/Library/Caches/ms-*",
    "rm -rf ${HOME}/Library/Caches",
    "rm -rf ~/.cache/ms-playwright ~/.cache/uv",
    "rm -rf ~/.cache",
    "rm -rf ~/Library",
    "rm -rf ~",
    "rm -rf -- ~/Library/Caches/ms-playwright",
    "trash ~/Library/Caches/ms-playwright",
    "mv ~/Library/Caches/ms-playwright /Volumes/SANDISK256/",
    "mv ~/Library/Caches /tmp/old-caches",
    "cd ~/Library/Caches && rm -rf ms-playwright",
    "ls && rm -rf ~/Library/Caches/ms-playwright",
    "ssh -i k root@49.12.7.18 'rm -rf /root/.cache/ms-playwright'",
    "ssh root@49.12.7.18 'rm -rf /root/.cache'",
    "ssh root@49.12.7.18 'rm -rf ~/.cache/ms-playwright'",
    "rm -rf /root/.cache/ms-playwright",
]
ALLOW = [
    "rm -rf ~/.cache/huggingface ~/.cache/uv ~/.cache/gh",
    "rm -rf ~/Library/Caches/semble",
    "rm -rf ~/Library/Caches/Codex",
    "rm -rf /tmp/scratch",
    "rm -rf node_modules",
    "ls ~/Library/Caches/ms-playwright",
    "du -sh ~/Library/Caches/*",
    "cp -R ~/Library/Caches/ms-playwright /Volumes/SANDISK256/",
    "mv ~/Downloads/a.png ~/Pictures/",
    "echo 'rm -rf ~/Library/Caches/ms-playwright'",
    "git commit -m 'never rm -rf ~/Library/Caches'",
    "ssh root@49.12.7.18 'rm -rf /root/.cache/uv'",
    "ssh root@49.12.7.18 'ls /root/.cache/ms-playwright'",
    "rm -rf $UNSET_VAR/x",
]


def test_denies_deleting_or_moving_the_playwright_cache_and_its_parents():
    for command in DENY:
        assert g.cache_delete_reason(command, "/Users/x/Developer/frankendom-deploy"), command


def test_allows_other_caches_reads_copies_and_quoted_text():
    for command in ALLOW:
        assert g.cache_delete_reason(command, "/Users/x/Developer/frankendom-deploy") == "", command


def test_deny_reaches_the_hook_output_on_the_mac():
    import io
    import json
    from unittest import mock

    import deploy_guard

    def run(command: str) -> str:
        payload = {"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": command}}
        out = io.StringIO()
        with mock.patch.object(deploy_guard, "_is_mac", return_value=True), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
            deploy_guard.main()
        return out.getvalue()

    denied = json.loads(run("rm -rf ~/Library/Caches/ms-playwright"))["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny" and denied["permissionDecisionReason"] == g.MESSAGE
    assert run("rm -rf ~/Library/Caches/semble") == ""


def test_message_names_the_rule():
    reason = g.cache_delete_reason("rm -rf ~/Library/Caches", "/tmp")
    assert "ms-playwright" in reason and "Lead" in reason

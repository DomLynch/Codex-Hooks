"""deploy_guard: every refusal leaves one line, so the next review can COUNT real catches against nuisances (Dom + Strategy, 2026-10-23: "is the
guard adding value?" could only be answered by guessing, because the token and cache checks kept no log of their blocks).

One line per block: UTC timestamp | rule (the first 50 characters of the reason) | the first 80 characters of the command, with anything token-shaped
masked first (the command text of a block can itself hold a token). Logging never costs a lane its refusal or its command."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy_guard  # noqa: E402

FAKE = "hf_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"[:34]   # token-shaped, not a real token


@pytest.fixture
def block_log(tmp_path, monkeypatch):
    path = tmp_path / "hook_blocks.log"
    monkeypatch.setattr(deploy_guard, "_block_log", lambda: path)
    return path


def test_a_block_writes_one_line_with_time_rule_and_command_start(block_log):
    deploy_guard.note_block("cat /root/.hf/token && echo " + "x" * 200, "A Hugging Face token never goes on a command line, in a commit")
    lines = block_log.read_text().splitlines()
    assert len(lines) == 1
    stamp, rule, command = [part.strip() for part in lines[0].split("|")]
    assert stamp.endswith("Z") and "T" in stamp
    assert rule.startswith("A Hugging Face token never goes on a command line")
    assert command.startswith("cat /root/.hf/token && echo xxx") and len(command) <= 80


def test_a_token_shaped_string_in_the_command_is_masked_in_the_log(block_log):
    deploy_guard.note_block(f"curl -H 'Authorization: Bearer {FAKE}' https://huggingface.co", "A Hugging Face token never goes on a command line")
    assert FAKE not in block_log.read_text()
    assert "hf_***" in block_log.read_text()


def test_logging_failure_never_breaks_the_hook(monkeypatch):
    def broken():
        raise OSError("disk full")
    monkeypatch.setattr(deploy_guard, "_block_log", broken)
    deploy_guard.note_block("cat /root/.hf/token", "reason")   # must not raise


def test_main_logs_the_refusal_it_returns(block_log, monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat /root/.hf/token"}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    deploy_guard.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "cat /root/.hf/token" in block_log.read_text()


def test_an_allowed_command_leaves_no_line(block_log, monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo hello"}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    deploy_guard.main()
    assert capsys.readouterr().out == ""
    assert not block_log.exists()

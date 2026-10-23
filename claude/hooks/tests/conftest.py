"""Puts claude/hooks on sys.path so the guard tests import `lib` the way the installed hooks do."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _block_log_in_tmp(tmp_path_factory, monkeypatch):
    """deploy_guard logs every refusal to ~/.claude/logs/hook_blocks.log so catches can be counted (Dom + Strategy, 2026-10-23). A test run must
    not write its fixture refusals into that real file: it would bury the real ones. Tests that look at the log patch `_block_log` themselves."""
    monkeypatch.setenv("HOOK_BLOCK_LOG", str(tmp_path_factory.mktemp("blocklog") / "hook_blocks.log"))   # also inherited by spawned hook processes

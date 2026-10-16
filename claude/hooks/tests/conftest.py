"""Test isolation for the Claude hooks suite (2026-10-16).

The judge throttle (#142) refuses to start a judge above machine load 10 and while another judge holds ~/.claude/state/judge.lock. Both are
facts about the machine, not about the code under test, so a test that reached the judge failed whenever a live reviewer ran in another
session or the Mac was busy (test_audit_a f11: red in the full suite, green alone). Every test gets its own lock file and a quiet load;
tests of the throttle itself (test_judge_gate.py) patch what they need on top, which still wins inside the test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_judge_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "JUDGE_LOCK_PATH", tmp_path / "judge.lock")
    monkeypatch.setattr(api, "judge_load1", lambda: 0.0)

"""2026-10-16: a test must never depend on the machine it runs on. The judge lock (#142) is a flock on the REAL ~/.claude/state/judge.lock,
so any test that called the judge raised JudgeBusy whenever a live reviewer in another session held it (or the load was over 10):
test_audit_a's f11 failed in the full suite while passing alone. conftest.py now gives every test its own lock file and a quiet load."""
from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib import api  # noqa: E402


def test_tests_get_their_own_judge_lock_not_the_real_one():
    assert api.JUDGE_LOCK_PATH != Path.home() / ".claude" / "state" / "judge.lock"
    assert "judge.lock" in api.JUDGE_LOCK_PATH.name


def test_tests_see_a_quiet_machine_regardless_of_real_load():
    assert api.judge_load1() == 0.0
    with api.judge_slot("test"):   # the slot is free: nothing else holds this test's own lock
        pass


def test_a_second_judge_in_the_same_test_still_hits_the_lock():
    import pytest
    with api.judge_slot("first"), pytest.raises(api.JudgeBusy):
        with api.judge_slot("second"):
            pass

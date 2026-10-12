"""Auditor on #129 (2026-10-12): heavy test runs that the bare Mac guard let through. A directory argument, ?/[ globs, a $( ) substitution that
expands to files, tsx --test, and a quote-split command name are all as heavy as the spelled-out form. Targeted `node --test a b` through the
lanejob wrapper stays allowed (test_lanejob_mac_exempt.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard as g  # noqa: E402


@pytest.mark.parametrize("cmd", [
    "node --test tests/",
    "node --test --test-concurrency=2 tests/",
    "npx tsx --test tests/*.test.ts",
    "npx tsx --test tests/a.test.ts tests/b.test.ts",
    "node --test tests/?.test.ts",
    "node --test tests/[a-z]*.ts",
    "node --test $(ls tests/*.ts)",
    "node --test `ls tests/*.ts`",
    "npx play''wright test",
    'npx play""wright test',
    "npx pla\\ywright test",
])
def test_these_were_allowed_bare_and_are_heavy(cmd):
    assert g.decide(cmd, None, cwd="") == g.VPS_REASON, cmd


@pytest.mark.parametrize("cmd", [
    "node --test tests/a.test.ts",
    "npx tsx --test tests/a.test.ts",
    "node scripts/vps-shadow/launch.mjs unit " + "a" * 40,   # only submits a Hugging Face job; blocking it would stop every PR receipt
    "echo 'it''s fine'",
    "git status",
])
def test_light_commands_stay_allowed(cmd):
    assert g.decide(cmd, None, cwd="") == "", cmd

"""Strategy + Dom, 2026-10-29: lanes spawn sub-agents with ad-hoc prompts and keep sending suites/builds to the VPS or the Mac because nothing tells the sub-agent about hfjob.
There is no shared template, so enforce at the Agent tool: when the prompt mentions test/suite/build/playwright/stills/blender, a PreToolUse hook appends the standing hfjob line
through updatedInput. Prompts that never mention heavy work, and prompts that already name hfjob, pass untouched."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import agent_brief_guard as g  # noqa: E402

HEAVY = [
    "Run the unit tests for the combat module and report failures.",
    "Build the site with vite and check the bundle size.",
    "Take stills of the boss at 375 wide with Playwright.",
    "Bake the rig in Blender and export the glb.",
    "Run the full suite on the VPS.",
    "run npm test then fix what breaks",
]
LIGHT = [
    "Read scripts/hfjob/hfjob.sh and summarise the flags.",
    "Find where the shield damage is computed and explain it.",
    "Review this diff for naming problems.",
    "",
    "Test-driven? no: just list the files that import lib/log.py",   # 'Test-' inside a hyphenated word is not a run request
]


def test_heavy_prompts_get_the_hfjob_line_appended():
    for prompt in HEAVY:
        out = g.updated_input({"prompt": prompt, "description": "x", "subagent_type": "general-purpose"})
        assert out is not None, prompt
        assert out["prompt"].startswith(prompt)
        assert "hfjob -- <cmd>" in out["prompt"] and "never on the VPS" in out["prompt"]
        # Strategy 2026-10-29 (Dom's GPU waterfall, 10-17): browser/WebGL proof rows are Mac first (real GPU); HF CPU SwiftShader cannot prove them (4 inconclusive runs)
        assert "Mac when its load is under 12" in out["prompt"] and "HF T4" in out["prompt"] and "one at a time" in out["prompt"]
        assert "never on the VPS or Mac directly" not in out["prompt"]
        assert "--filter=blob:none --depth 1 --single-branch" in out["prompt"]
        assert out["description"] == "x" and out["subagent_type"] == "general-purpose"


def test_light_prompts_and_prompts_that_already_say_hfjob_pass_untouched():
    for prompt in LIGHT:
        assert g.updated_input({"prompt": prompt}) is None, prompt
    assert g.updated_input({"prompt": "Run the tests via hfjob -- npm test and report."}) is None
    assert g.updated_input({}) is None
    assert g.updated_input({"prompt": 5}) is None


def run_main(payload):
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
        g.main()
    return out.getvalue()


def test_main_emits_updatedinput_allow_for_agent_calls_only():
    res = json.loads(run_main({"tool_name": "Agent", "tool_input": {"prompt": "Run the test suite", "description": "d"}}))["hookSpecificOutput"]
    assert res["hookEventName"] == "PreToolUse" and res["permissionDecision"] == "allow"
    assert "hfjob -- <cmd>" in res["updatedInput"]["prompt"] and res["updatedInput"]["description"] == "d"
    assert run_main({"tool_name": "Agent", "tool_input": {"prompt": "Read the README"}}) == ""
    assert run_main({"tool_name": "Bash", "tool_input": {"command": "npm test"}}) == ""
    assert run_main({}) == ""

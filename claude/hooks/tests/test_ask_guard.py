"""ask_guard: AskUserQuestion is denied in every lane (Dom, 2026-10-16): a blocking pop-up froze PicoClaw overnight
while Dom was away. Every other tool, and any odd payload, passes untouched."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_guard as g  # noqa: E402

QUESTION = {"questions": [{"question": "Stop the Codex jobs?", "header": "Jobs", "multiSelect": False,
                           "options": [{"label": "Yes", "description": "stop"}, {"label": "No", "description": "keep"}]}]}


def run(payload) -> str:
    out = io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    with mock.patch.object(sys, "stdin", io.StringIO(raw)), redirect_stdout(out):
        g.main()
    return out.getvalue()


def test_ask_user_question_is_denied_in_every_lane():
    for cwd in ("/Users/x/Developer/frankendom-lead-catalogue", "/Users/x/Developer/picoclaw",
                "/Users/x/Desktop/Business/Codex Hooks", "/Users/x/Developer/frankendom-zone-sandbox", ""):
        out = json.loads(run({"tool_name": "AskUserQuestion", "tool_input": QUESTION, "cwd": cwd}))
        decision = out["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse" and decision["permissionDecision"] == "deny", cwd
        assert "No blocking questions (Dom 10-16)" in decision["permissionDecisionReason"]
        # Dom 2026-10-16 ~00:4x via Strategy: questions never go to Dom; a ruling goes to Lead or Strategy by message
        reason = decision["permissionDecisionReason"]
        assert "Pick the conservative default and continue." in reason
        assert "'Frankendom - Lead Dev'" in reason and "'Project Strategy - Fable 5.1'" in reason
        assert "never wait on Dom" in reason


WRAP_UP = "mcp__ccd_turn__wrap_up"


def test_wrap_up_with_a_question_or_buttons_is_denied():
    """2026-10-17 (Strategy, from Dom's screenshot): "Combat/character sessions v2" showed "Which spell should I rebuild first?" with
    Nyx/Ash Fall buttons. That card is not AskUserQuestion: it is the app's wrap_up tool carrying `ask` and `actions`."""
    for tool_input in ({"recap": "done", "ask": "Which spell first?", "actions": [{"label": "Nyx first", "message": "go"}]},
                       {"recap": "done", "ask": "Which spell first?"},
                       {"recap": "done", "actions": [{"label": "Ship", "message": "ship"}]}):
        out = json.loads(run({"tool_name": WRAP_UP, "tool_input": tool_input}))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", tool_input
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "recap" in reason and "any future one" in reason and "No blocking questions (Dom 10-16)" in reason


def test_a_plain_wrap_up_recap_still_passes():
    for tool_input in ({"recap": "All merged."}, {"recap": "x", "ask": "", "actions": []}, {"recap": "x", "ask": None}, {}):
        assert run({"tool_name": WRAP_UP, "tool_input": tool_input}) == "", tool_input


def test_other_tools_and_odd_payloads_pass():
    for payload in ({"tool_name": "Bash", "tool_input": {"command": "ls"}}, {"tool_name": "Read", "tool_input": {}},
                    {"tool_name": "AskUserQuestionX"}, {}, "not json", ""):
        assert run(payload) == "", payload



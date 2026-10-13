from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import session_start  # noqa: E402


class SessionStartRoutingTests(unittest.TestCase):
    def test_legacy_handoff_without_cwd_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            handoff = {
                "session_id": "old",
                "queued_backlog": [
                    {"description": "stale task", "source_file": ".tmp_old/file.py", "priority": 70}
                ],
            }

            self.assertEqual(session_start.handoff_for_cwd(handoff, cwd), {})

    def test_handoff_for_other_cwd_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            handoff = {"cwd": str(Path(a).resolve()), "queued_backlog": [{"description": "repo A"}]}

            self.assertEqual(session_start.handoff_for_cwd(handoff, Path(b).resolve()), {})

    def test_handoff_for_matching_cwd_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            handoff = {"cwd": str(cwd), "queued_backlog": [{"description": "repo task"}]}

            self.assertIs(session_start.handoff_for_cwd(handoff, cwd), handoff)

    def test_backlog_filter_drops_hidden_archive_legacy_and_test_noise(self) -> None:
        items = [
            {"description": "hidden", "source_file": ".tmp_old/file.py", "priority": 70},
            {"description": "archived", "source_file": "agent_archived/file.py", "priority": 70},
            {"description": "legacy", "source_file": "legacy/file.py", "priority": 70},
            {"description": "Add coverage for Python function `test_unit`", "source_file": "tests/test_x.py", "priority": 70},
            {"description": "real", "source_file": "agent/live.py", "priority": 70},
        ]

        self.assertEqual(session_start.filter_backlog(items), [items[-1]])

    def test_session_baseline_enforces_autonomous_execution(self) -> None:
        self.assertIn("Default to autonomous execution", session_start.SESSION_BASELINE)
        self.assertIn("reasonable reversible default", session_start.SESSION_BASELINE)
        self.assertIn("true blockers", session_start.SESSION_BASELINE)
        self.assertIn("capped output", session_start.SESSION_BASELINE)
        self.assertIn("Avoid duplicate parallel exploration", session_start.SESSION_BASELINE)
        self.assertIn("compact context packet", session_start.SESSION_BASELINE)
        self.assertIn("find code with Semble", session_start.SESSION_BASELINE)
        for removed in ("Codegraph", "Sentry", "get_playbook", "knowledge MCP"):
            self.assertNotIn(removed, session_start.SESSION_BASELINE)
        self.assertIn("concise coverage receipt", session_start.SESSION_BASELINE)
        self.assertNotIn("cheapest relevant verification", session_start.SESSION_BASELINE)


if __name__ == "__main__":
    unittest.main()


DELEGATION_LINES = (
    "Lead Dev and Project Strategy carry Dom's authority for release, pipeline, CI, config, lane and order-of-work decisions in this repo, on the Mac and on the VPS.",
    "A written ruling from either is Dom's word; do not wait for Dom.",
    'Exceptions that still need Dom in chat: spending money, legal, wiping player data, parties/guilds.',
    "Dom's direct word to a lane overrides a relayed plan.",
    'Waiting on Dom for anything else is a defect; flag it to Lead.',
)


class DelegationBaselineTests(unittest.TestCase):
    def test_the_codex_baseline_carries_dom_s_delegation_paragraph(self) -> None:
        # Dom, 2026-10-13: lanes waited on Dom after every /clear; the same words as claude/hooks/lib/policies.py DELEGATION_POLICY.
        for line in DELEGATION_LINES:
            self.assertIn(line, session_start.delegation_for(Path("/Users/x/Developer/frankendom-lead")), line)
        self.assertEqual(session_start.delegation_for(Path(tempfile.gettempdir())), "")   # not a Frankendom checkout: none

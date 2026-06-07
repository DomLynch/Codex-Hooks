from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import session_start  # noqa: E402
from lib.quality import mine_backlog  # noqa: E402


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
        self.assertNotIn("cheapest relevant verification", session_start.SESSION_BASELINE)

    def test_mine_backlog_ignores_marker_config_and_prose_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "config.py").write_text('MARKERS = "TODO|FIXME|XXX"\n')
            (cwd / "notes.md").write_text("Mention TODO/FIXME/XXX without assigning work.\n")

            self.assertEqual(mine_backlog(cwd), [])

    def test_mine_backlog_accepts_owner_marker_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "hook.py").write_text("# FIXME(owner): tighten scanner\n")

            items = mine_backlog(cwd)

        self.assertEqual(items[0]["source_file"], "hook.py:1")
        self.assertIn("FIXME(owner): tighten scanner", items[0]["description"])


if __name__ == "__main__":
    unittest.main()

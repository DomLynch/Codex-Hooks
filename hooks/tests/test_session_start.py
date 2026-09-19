from __future__ import annotations

import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch


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
        self.assertIn("three materially different Semble searches", session_start.SESSION_BASELINE)
        self.assertIn("together, not as alternatives", session_start.SESSION_BASELINE)
        self.assertIn("only then degrade to Semble + `rg`", session_start.SESSION_BASELINE)
        self.assertIn("concise coverage receipt", session_start.SESSION_BASELINE)
        self.assertNotIn("cheapest relevant verification", session_start.SESSION_BASELINE)

    def test_codegraph_notice_warns_only_for_indexless_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            self.assertEqual(session_start.codegraph_notice(str(cwd)), "")
            subprocess.run(["git", "init"], cwd=cwd, check=True, capture_output=True)
            with patch.object(session_start.shutil, "which", return_value=None):
                self.assertIn("initialization failed or is unavailable", session_start.codegraph_notice(str(cwd)))
            (cwd / ".codegraph").mkdir()
            (cwd / ".codegraph" / "codegraph.db").write_text("")
            self.assertEqual(session_start.codegraph_notice(str(cwd)), "")

    def test_codegraph_notice_initializes_missing_index_at_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            nested = root / "src"
            nested.mkdir()
            def initialize(*_args, **_kwargs):
                (root / ".codegraph").mkdir()
                (root / ".codegraph" / "codegraph.db").write_text("")
                return Mock(returncode=0)

            with patch.object(session_start, "git_root", return_value=root), \
                 patch.object(session_start.shutil, "which", return_value="/usr/bin/codegraph"), \
                 patch.object(session_start.subprocess, "run", side_effect=initialize) as run, \
                 patch.object(session_start, "log"):
                self.assertEqual(session_start.codegraph_notice(str(nested)), "")
                run.assert_called_once_with(["codegraph", "init"], cwd=root, capture_output=True, text=True, timeout=20)
                self.assertEqual(session_start.codegraph_notice(str(nested)), "")
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()

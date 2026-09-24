import io
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codegraph_freshness  # noqa: E402


def indexed(root: Path) -> None:
    (root / ".git").mkdir()
    (root / ".codegraph").mkdir(parents=True)
    (root / ".codegraph" / "codegraph.db").touch()


def invoke(payload: dict) -> str:
    with (mock.patch.object(codegraph_freshness.sys, "stdin", io.StringIO(json.dumps(payload))),
          mock.patch.object(codegraph_freshness.sys, "stdout", io.StringIO()) as output):
        codegraph_freshness.main()
        return output.getvalue()


class CodegraphFreshnessTests(unittest.TestCase):
    def test_example_config_registers_pre_query_hook(self) -> None:
        config = tomllib.loads((Path(__file__).resolve().parents[2] / "config" / "hooks.example.toml").read_text())
        # Looked up by matcher, not by index: this asserted PreToolUse[0] until 2026-09-23, so adding any other
        # PreToolUse hook (the deploy guard) failed it for a reason that had nothing to do with codegraph.
        groups = [g for g in config["hooks"]["PreToolUse"]
                  if g.get("matcher") == "^mcp__codegraph__codegraph_explore$"]
        self.assertEqual(len(groups), 1)
        self.assertIn("codegraph_freshness.py", groups[0]["hooks"][0]["command"])

    def test_refreshes_nearest_index_not_session_cwd_or_outer_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            nested = outer / "service"
            (nested / "src").mkdir(parents=True)
            indexed(outer)
            indexed(nested)
            payload = {"tool_name": "mcp__codegraph__codegraph_explore", "cwd": str(outer),
                       "tool_input": {"projectPath": str(nested / "src")}}
            with mock.patch.object(codegraph_freshness.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(invoke(payload), "")
            run.assert_called_once_with(["codegraph", "sync", "-q"], cwd=nested.resolve(), capture_output=True, text=True, timeout=30)

    def test_unrelated_tool_and_missing_index_do_not_sync(self) -> None:
        with tempfile.TemporaryDirectory() as indexed_tmp, tempfile.TemporaryDirectory() as missing_tmp:
            root = Path(indexed_tmp)
            indexed(root)
            with mock.patch.object(codegraph_freshness.subprocess, "run") as run:
                self.assertEqual(invoke({"tool_name": "Bash", "cwd": str(root)}), "")
                decision = json.loads(invoke({"tool_name": "mcp__codegraph__codegraph_explore",
                                              "tool_input": {"projectPath": missing_tmp}}))["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")
            run.assert_not_called()

    def test_never_falls_back_to_business_index_for_unindexed_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            business = Path(tmp)
            worktree = business / "game-worktree"
            worktree.mkdir()
            indexed(business)
            (worktree / ".git").touch()  # Linked worktrees use a .git file.
            payload = {"tool_name": "mcp__codegraph__codegraph_explore", "cwd": str(business),
                       "tool_input": {"projectPath": str(worktree)}}
            with mock.patch.object(codegraph_freshness.subprocess, "run") as run:
                decision = json.loads(invoke(payload))["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn(str(worktree), decision["permissionDecisionReason"])
            run.assert_not_called()

    def test_business_cwd_without_project_path_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            business = Path(tmp)
            (business / ".codegraph").mkdir()
            (business / ".codegraph" / "codegraph.db").touch()
            with mock.patch.object(codegraph_freshness.subprocess, "run") as run:
                decision = json.loads(invoke({"tool_name": "mcp__codegraph__codegraph_explore",
                                              "cwd": str(business)}))["hookSpecificOutput"]
                explicit = json.loads(invoke({"tool_name": "mcp__codegraph__codegraph_explore",
                                              "tool_input": {"projectPath": str(business)}}))["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("projectPath", decision["permissionDecisionReason"])
            self.assertEqual(explicit["permissionDecision"], "deny")
            run.assert_not_called()

    def test_failed_sync_denies_stale_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            indexed(root)
            payload = {"tool_name": "mcp__codegraph__codegraph_explore",
                       "tool_input": {"projectPath": str(root)}}
            with mock.patch.object(codegraph_freshness.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                decision = json.loads(invoke(payload))["hookSpecificOutput"]
            self.assertEqual(decision["hookEventName"], "PreToolUse")
            self.assertEqual(decision["permissionDecision"], "deny")
            with mock.patch.object(codegraph_freshness.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 30)):
                decision = json.loads(invoke(payload))["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")

    def test_malformed_payload_fails_open(self) -> None:
        for payload in ("{bad json", "[]"):
            with (mock.patch.object(codegraph_freshness.sys, "stdin", io.StringIO(payload)),
                  mock.patch.object(codegraph_freshness.subprocess, "run") as run):
                codegraph_freshness.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

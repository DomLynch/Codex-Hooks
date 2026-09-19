import io
import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codegraph_freshness  # noqa: E402


class CodegraphFreshnessTests(unittest.TestCase):
    def test_example_config_registers_pre_query_hook(self) -> None:
        config = tomllib.loads((Path(__file__).resolve().parents[2] / "config" / "hooks.example.toml").read_text())
        groups = config["hooks"]["PreToolUse"]
        self.assertEqual(groups[0]["matcher"], "^mcp__codegraph__codegraph_explore$")
        self.assertIn("codegraph_freshness.py", groups[0]["hooks"][0]["command"])

    def test_refreshes_requested_worktree_before_explore(self) -> None:
        root = Path("/tmp/indexed")
        payload = {"tool_name": "mcp__codegraph__codegraph_explore", "cwd": "/tmp/other",
                   "tool_input": {"projectPath": str(root / "src")}}
        with (mock.patch.object(codegraph_freshness.sys, "stdin", io.StringIO(json.dumps(payload))),
              mock.patch.object(codegraph_freshness, "git_root", return_value=root),
              mock.patch.object(Path, "is_file", return_value=True),
              mock.patch.object(codegraph_freshness.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
              mock.patch.object(codegraph_freshness.sys, "stdout", io.StringIO()) as output):
            codegraph_freshness.main()
        run.assert_called_once_with(["codegraph", "sync", "-q"], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(output.getvalue(), "")

    def test_unrelated_tool_and_missing_index_do_not_sync(self) -> None:
        for tool_name, indexed in (("Bash", True), ("mcp__codegraph__codegraph_explore", False)):
            with self.subTest(tool_name=tool_name, indexed=indexed):
                payload = {"tool_name": tool_name, "cwd": "/tmp/repo", "tool_input": {}}
                with (mock.patch.object(codegraph_freshness.sys, "stdin", io.StringIO(json.dumps(payload))),
                      mock.patch.object(codegraph_freshness, "git_root", return_value=Path("/tmp/repo")),
                      mock.patch.object(Path, "is_file", return_value=indexed),
                      mock.patch.object(codegraph_freshness.subprocess, "run") as run):
                    codegraph_freshness.main()
                run.assert_not_called()

    def test_failed_sync_denies_stale_query(self) -> None:
        payload = {"tool_name": "mcp__codegraph__codegraph_explore", "cwd": "/tmp/repo"}
        with (mock.patch.object(codegraph_freshness.sys, "stdin", io.StringIO(json.dumps(payload))),
              mock.patch.object(codegraph_freshness, "git_root", return_value=Path("/tmp/repo")),
              mock.patch.object(Path, "is_file", return_value=True),
              mock.patch.object(codegraph_freshness.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)),
              mock.patch.object(codegraph_freshness.sys, "stdout", io.StringIO()) as output):
            codegraph_freshness.main()
        decision = json.loads(output.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_timeout_denies_stale_query(self) -> None:
        payload = {"tool_name": "mcp__codegraph__codegraph_explore", "cwd": "/tmp/repo"}
        with (mock.patch.object(codegraph_freshness.sys, "stdin", io.StringIO(json.dumps(payload))),
              mock.patch.object(codegraph_freshness, "git_root", return_value=Path("/tmp/repo")),
              mock.patch.object(Path, "is_file", return_value=True),
              mock.patch.object(codegraph_freshness.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 30)),
              mock.patch.object(codegraph_freshness.sys, "stdout", io.StringIO()) as output):
            codegraph_freshness.main()
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_malformed_payload_fails_open(self) -> None:
        for payload in ("{bad json", "[]"):
            with (mock.patch.object(codegraph_freshness.sys, "stdin", io.StringIO(payload)),
                  mock.patch.object(codegraph_freshness.subprocess, "run") as run):
                codegraph_freshness.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

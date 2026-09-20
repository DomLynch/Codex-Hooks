from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "codegraph_freshness.py"
SPEC = importlib.util.spec_from_file_location("codegraph_freshness_under_test", MODULE)
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def run(payload: dict) -> str:
    buf = io.StringIO()
    with mock.patch.object(hook, "log"), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(buf):
        hook.main()
    return buf.getvalue()


class CodegraphFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".codegraph").mkdir()
        (self.root / ".codegraph" / "codegraph.db").write_bytes(b"")
        (self.root / "src").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_other_tools_are_ignored(self) -> None:
        with mock.patch.object(subprocess, "run") as run_:
            self.assertEqual(run({"tool_name": "Bash", "cwd": str(self.root)}), "")
            run_.assert_not_called()

    def test_syncs_nearest_index_above_project_path_and_stays_silent_on_success(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run_:
            out = run({"tool_name": hook.TOOL, "cwd": "/elsewhere", "tool_input": {"projectPath": str(self.root / "src")}})
        self.assertEqual(out, "")
        self.assertEqual(run_.call_args.args[0], ["codegraph", "sync", "-q"])
        self.assertEqual(Path(run_.call_args.kwargs["cwd"]).resolve(), self.root.resolve())

    def test_falls_back_to_cwd_when_no_project_path(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run_:
            run({"tool_name": hook.TOOL, "cwd": str(self.root / "src"), "tool_input": {"query": "x"}})
        self.assertEqual(Path(run_.call_args.kwargs["cwd"]).resolve(), self.root.resolve())

    def test_no_index_means_no_sync_and_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as bare, mock.patch.object(subprocess, "run") as run_:
            self.assertEqual(run({"tool_name": hook.TOOL, "cwd": bare, "tool_input": {"query": "x"}}), "")
            run_.assert_not_called()

    def test_failed_or_hung_sync_denies_the_read(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 2)):
            out = json.loads(run({"tool_name": hook.TOOL, "cwd": str(self.root), "tool_input": {}}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("exit 2", out["hookSpecificOutput"]["permissionDecisionReason"])
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("codegraph", 30)):
            out = json.loads(run({"tool_name": hook.TOOL, "cwd": str(self.root), "tool_input": {}}))
        self.assertIn("TimeoutExpired", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_audit_child_exits_before_doing_anything(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_HOOK_AUDIT_CHILD": "1"}), mock.patch.object(subprocess, "run") as run_:
            with self.assertRaises(SystemExit):
                run({"tool_name": hook.TOOL, "cwd": str(self.root), "tool_input": {}})
            run_.assert_not_called()


if __name__ == "__main__":
    unittest.main()

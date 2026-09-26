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
import time
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
        with mock.patch.object(hook, "sync") as sync:
            self.assertEqual(run({"tool_name": "Bash", "cwd": str(self.root)}), "")
            sync.assert_not_called()

    def test_syncs_nearest_index_above_project_path_and_stays_silent_on_success(self) -> None:
        with mock.patch.object(hook, "sync", return_value=0) as sync:
            out = run({"tool_name": hook.TOOL, "cwd": "/elsewhere", "tool_input": {"projectPath": str(self.root / "src")}})
        self.assertEqual(out, "")
        self.assertEqual(sync.call_args.args[0].resolve(), self.root.resolve())

    def test_falls_back_to_cwd_when_no_project_path(self) -> None:
        with mock.patch.object(hook, "sync", return_value=0) as sync:
            run({"tool_name": hook.TOOL, "cwd": str(self.root / "src"), "tool_input": {"query": "x"}})
        self.assertEqual(sync.call_args.args[0].resolve(), self.root.resolve())

    def test_no_index_means_no_sync_and_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as bare, mock.patch.object(hook, "sync") as sync:
            self.assertEqual(run({"tool_name": hook.TOOL, "cwd": bare, "tool_input": {"query": "x"}}), "")
            sync.assert_not_called()

    def test_failed_or_hung_sync_denies_the_read(self) -> None:
        with mock.patch.object(hook, "sync", return_value=2):
            out = json.loads(run({"tool_name": hook.TOOL, "cwd": str(self.root), "tool_input": {}}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("exit 2", out["hookSpecificOutput"]["permissionDecisionReason"])
        with mock.patch.object(hook, "sync", side_effect=subprocess.TimeoutExpired("codegraph", 30)):
            out = json.loads(run({"tool_name": hook.TOOL, "cwd": str(self.root), "tool_input": {}}))
        self.assertIn("TimeoutExpired", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_index_root_above_the_git_toplevel_is_never_synced(self) -> None:
        # A Frankendom worktree under the Business workspace: the repo has no index of its own,
        # the workspace root has an 18 GB one. The read proceeds, the sync does not.
        repo = self.root / "frankendom" / "worktree"
        (repo / "src").mkdir(parents=True)
        (repo / ".git").write_text("gitdir: /elsewhere\n")
        with mock.patch.object(hook, "sync") as sync:
            self.assertEqual(run({"tool_name": hook.TOOL, "cwd": str(repo / "src"), "tool_input": {}}), "")
            sync.assert_not_called()

    def test_index_inside_the_repo_still_syncs(self) -> None:
        (self.root / ".git").mkdir()
        with mock.patch.object(hook, "sync", return_value=0) as sync:
            run({"tool_name": hook.TOOL, "cwd": str(self.root / "src"), "tool_input": {}})
        self.assertEqual(sync.call_args.args[0].resolve(), self.root.resolve())

    def test_timeout_kills_the_grandchild_too(self) -> None:
        # The real `codegraph` is a node launcher whose child does the work; killing only the
        # launcher left `codegraph sync -q` (pid 5274) grinding for 16 h on 2026-09-26.
        pidfile = self.root / "grandchild.pid"
        cmd = ["sh", "-c", f"sh -c 'echo $$ > {pidfile}; sleep 60' & wait"]
        with mock.patch.object(hook, "SYNC_CMD", cmd), mock.patch.object(hook, "SYNC_TIMEOUT_S", 0.5):
            with self.assertRaises(subprocess.TimeoutExpired):
                hook.sync(self.root)
        grandchild = int(pidfile.read_text())
        for _ in range(40):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(grandchild, 9)
            self.fail(f"grandchild {grandchild} survived the timeout")

    def test_audit_child_exits_before_doing_anything(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_HOOK_AUDIT_CHILD": "1"}), mock.patch.object(hook, "sync") as sync:
            with self.assertRaises(SystemExit):
                run({"tool_name": hook.TOOL, "cwd": str(self.root), "tool_input": {}})
            sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()

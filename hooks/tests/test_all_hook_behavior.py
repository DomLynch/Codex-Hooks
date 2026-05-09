from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import mimo_audit  # noqa: E402
import post_tool  # noqa: E402
import stop_orchestrator  # noqa: E402
import user_prompt_submit  # noqa: E402


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=True,
    )


def init_git_repo(cwd: Path) -> None:
    git(cwd, "init")
    git(cwd, "config", "user.email", "hooks-test@example.com")
    git(cwd, "config", "user.name", "Hooks Test")


def run_main_with_stdin(fn, payload: dict) -> tuple[int | None, str]:
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    with mock.patch("sys.stdin", stdin), redirect_stdout(stdout):
        try:
            fn()
        except SystemExit as exc:
            return int(exc.code or 0), stdout.getvalue()
    return None, stdout.getvalue()


class AllHookBehaviorTests(unittest.TestCase):
    def test_user_prompt_submit_injects_v4_for_project_work(self) -> None:
        with mock.patch.object(user_prompt_submit, "log"):
            rc, out = run_main_with_stdin(
                user_prompt_submit.main,
                {"prompt": "fix the hook tests", "cwd": "/tmp/project"},
            )

        data = json.loads(out)
        context = data["hookSpecificOutput"]["additionalContext"]
        self.assertIsNone(rc)
        self.assertIn("V4/MCP Task Routing Contract", context)
        self.assertIn("get_playbook", context)
        self.assertIn("Autonomous Execution Contract", context)
        self.assertIn("Do not ask for confirmation", context)
        self.assertIn("reasonable reversible default", context)
        self.assertNotIn("cheapest relevant verification", context)

    def test_simple_prompt_still_gets_no_babysit_policy(self) -> None:
        with mock.patch.object(user_prompt_submit, "log"):
            rc, out = run_main_with_stdin(
                user_prompt_submit.main,
                {"prompt": "what is the shortest answer here?", "cwd": "/tmp/project"},
            )

        data = json.loads(out)
        context = data["hookSpecificOutput"]["additionalContext"]
        self.assertIsNone(rc)
        self.assertIn("Current-Prompt Discipline", context)
        self.assertIn("Autonomous Execution Contract", context)
        self.assertIn("Ask the user only for true blockers", context)
        self.assertIn("whether to continue", context)

    def test_mimo_audit_prompt_blocks_unnecessary_clarifications(self) -> None:
        self.assertIn("Unnecessary clarification", mimo_audit.AUDIT_PROMPT)
        self.assertIn("reasonable reversible default", mimo_audit.AUDIT_PROMPT)

    def test_mimo_audit_prompt_blocks_metadata_only_false_alarms(self) -> None:
        self.assertIn('what counts as "task content"', mimo_audit.AUDIT_PROMPT)
        self.assertIn("metadata only", mimo_audit.AUDIT_PROMPT)
        self.assertIn("unable to audit", mimo_audit.AUDIT_PROMPT)
        self.assertIn("audit THAT content on its merits", mimo_audit.AUDIT_PROMPT)

    def test_post_tool_blocks_syntax_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.py"
            path.write_text("def broken(:\n")
            payload = {
                "session_id": "test-post-tool",
                "cwd": tmp,
                "tool_name": "Write",
                "tool_input": {"file_path": str(path)},
            }

            with mock.patch.object(post_tool, "log"):
                rc, out = run_main_with_stdin(post_tool.main, payload)

        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["decision"], "block")
        self.assertIn("py_compile failed", data["reason"])

    def test_post_tool_non_edit_checks_existing_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            path = repo / "module.py"
            path.write_text("x = 1\n")
            git(repo, "add", "module.py")
            git(repo, "commit", "-m", "initial")
            path.write_text("def broken(:\n")
            payload = {
                "session_id": "test-post-tool-bash",
                "cwd": str(repo),
                "tool_name": "Bash",
                "tool_input": {"cmd": "true"},
            }

            with mock.patch.object(post_tool, "log"):
                rc, out = run_main_with_stdin(post_tool.main, payload)

        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["decision"], "block")
        self.assertIn("py_compile failed", data["reason"])

    def test_post_tool_periodic_mimo_checkpoint_blocks_on_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "post_tool_mimo_audit.json"
            file_path = tmp_path / "module.py"
            file_path.write_text("x = 1\n")
            state_path.write_text(json.dumps({
                "session-midrun": {
                    "edits_since_last_audit": 4,
                    "last_audit_at": "2026-05-09T00:00:00+00:00",
                }
            }))
            payload = {
                "session_id": "session-midrun",
                "cwd": tmp,
                "tool_name": "Write",
                "tool_input": {"file_path": str(file_path)},
                "last_assistant_message": "Completed useful code changes. " * 40,
            }

            with (
                mock.patch.object(post_tool, "MIMO_CHECKPOINT_AUDIT_PATH", state_path),
                mock.patch.object(post_tool, "load_api_key", return_value="test-key"),
                mock.patch.object(post_tool, "call_mimo", return_value="VERDICT: ISSUES\n1. Fix the incomplete implementation."),
                mock.patch.object(post_tool, "maybe_checkpoint") as checkpoint,
                mock.patch.object(post_tool, "log"),
            ):
                rc, out = run_main_with_stdin(post_tool.main, payload)

            data = json.loads(out)
            state = json.loads(state_path.read_text())["session-midrun"]
            checkpoint.assert_not_called()

        self.assertEqual(rc, 0)
        self.assertEqual(data["decision"], "block")
        self.assertIn("[MiMo checkpoint audit]", data["reason"])
        self.assertIn("Fix the incomplete implementation", data["reason"])
        self.assertEqual(state["edits_since_last_audit"], 0)
        self.assertEqual(state["last_verdict"], "ISSUES")

    def test_post_tool_periodic_mimo_checkpoint_pass_resets_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "post_tool_mimo_audit.json"
            file_path = tmp_path / "module.py"
            file_path.write_text("x = 1\n")
            state_path.write_text(json.dumps({
                "session-midrun": {
                    "edits_since_last_audit": 4,
                    "last_audit_at": "2026-05-09T00:00:00+00:00",
                }
            }))
            payload = {
                "session_id": "session-midrun",
                "cwd": tmp,
                "tool_name": "Write",
                "tool_input": {"file_path": str(file_path)},
                "last_assistant_message": "Completed useful code changes. " * 40,
            }

            with (
                mock.patch.object(post_tool, "MIMO_CHECKPOINT_AUDIT_PATH", state_path),
                mock.patch.object(post_tool, "load_api_key", return_value="test-key"),
                mock.patch.object(post_tool, "call_mimo", return_value="VERDICT: PASS"),
                mock.patch.object(post_tool, "maybe_checkpoint") as checkpoint,
                mock.patch.object(post_tool, "log"),
            ):
                rc, out = run_main_with_stdin(post_tool.main, payload)

            state = json.loads(state_path.read_text())["session-midrun"]
            checkpoint.assert_called_once()

        self.assertIsNone(rc)
        self.assertEqual(out, "")
        self.assertEqual(state["edits_since_last_audit"], 0)
        self.assertEqual(state["last_verdict"], "PASS")

    def test_post_tool_periodic_mimo_checkpoint_not_due_skips_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "post_tool_mimo_audit.json"
            file_path = tmp_path / "module.py"
            file_path.write_text("x = 1\n")
            state_path.write_text(json.dumps({
                "session-midrun": {
                    "edits_since_last_audit": 1,
                    "last_audit_at": post_tool.now_iso(),
                }
            }))
            payload = {
                "session_id": "session-midrun",
                "cwd": tmp,
                "tool_name": "Write",
                "tool_input": {"file_path": str(file_path)},
                "last_assistant_message": "Completed useful code changes. " * 40,
            }

            with (
                mock.patch.object(post_tool, "MIMO_CHECKPOINT_AUDIT_PATH", state_path),
                mock.patch.object(post_tool, "call_mimo") as call_mimo,
                mock.patch.object(post_tool, "maybe_checkpoint"),
                mock.patch.object(post_tool, "log"),
            ):
                rc, out = run_main_with_stdin(post_tool.main, payload)

        self.assertIsNone(rc)
        self.assertEqual(out, "")
        call_mimo.assert_not_called()

    def test_post_tool_periodic_mimo_checkpoint_api_error_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "post_tool_mimo_audit.json"
            file_path = tmp_path / "module.py"
            file_path.write_text("x = 1\n")
            state_path.write_text(json.dumps({
                "session-midrun": {
                    "edits_since_last_audit": 4,
                    "last_audit_at": "2026-05-09T00:00:00+00:00",
                }
            }))
            payload = {
                "session_id": "session-midrun",
                "cwd": tmp,
                "tool_name": "Write",
                "tool_input": {"file_path": str(file_path)},
                "last_assistant_message": "Completed useful code changes. " * 40,
            }

            with (
                mock.patch.object(post_tool, "MIMO_CHECKPOINT_AUDIT_PATH", state_path),
                mock.patch.object(post_tool, "load_api_key", return_value="test-key"),
                mock.patch.object(post_tool, "call_mimo", side_effect=TimeoutError("timeout")),
                mock.patch.object(post_tool, "maybe_checkpoint") as checkpoint,
                mock.patch.object(post_tool, "log"),
            ):
                rc, out = run_main_with_stdin(post_tool.main, payload)

            state = json.loads(state_path.read_text())["session-midrun"]
            checkpoint.assert_called_once()

        self.assertIsNone(rc)
        self.assertEqual(out, "")
        self.assertEqual(state["last_verdict"], "ERROR")

    def test_mimo_audit_pass_path_saves_pass_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "mimo-test-session",
                "turn_id": "turn-1",
                "last_assistant_message": "Completed task. " * 40,
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "load_api_key", return_value="test-key"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS"),
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(verdict["mimo-test-session"]["verdict"], "PASS")
        self.assertEqual(verdict["mimo-test-session"]["turn_id"], "turn-1")

    def test_mimo_short_transcript_with_tool_activity_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "mimo-tool-short",
                "turn_id": "turn-1",
                "last_assistant_message": "TOOL CALL:\nEdit: app.py",
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "load_api_key", return_value="test-key"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as ask_mimo,
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        ask_mimo.assert_called_once()
        self.assertEqual(verdict["mimo-tool-short"]["verdict"], "PASS")

    def test_mimo_short_transcript_without_tool_activity_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "mimo-chat-short",
                "turn_id": "turn-1",
                "last_assistant_message": "ok",
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "load_api_key", return_value="test-key"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry") as ask_mimo,
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        ask_mimo.assert_not_called()
        self.assertEqual(verdict["mimo-chat-short"]["verdict"], "SKIP_TOO_SHORT")

    def test_mimo_iterations_are_keyed_by_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "iterations.json"
            state_path.write_text(json.dumps({"session-a": 3}))
            with mock.patch.object(mimo_audit, "STATE_FILE", state_path):
                mimo_audit.save_iterations("session-a", "turn-1", 2)

                self.assertEqual(mimo_audit.load_iterations("session-a", "turn-1"), 2)
                self.assertEqual(mimo_audit.load_iterations("session-a", "turn-2"), 0)
                data = json.loads(state_path.read_text())
                self.assertNotIn("session-a", data)

    def test_stop_orchestrator_blocks_side_effects_without_mimo_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict_path = Path(tmp) / "mimo_last_verdict.json"
            verdict_path.write_text(json.dumps({
                "orch-session": {
                    "turn_id": "turn-1",
                    "verdict": "ISSUES",
                    "updated_at": 9999999999,
                }
            }))
            with (
                mock.patch.object(stop_orchestrator, "MIMO_VERDICT_PATH", verdict_path),
                mock.patch.object(stop_orchestrator, "log"),
            ):
                self.assertFalse(stop_orchestrator.wait_for_mimo_pass("orch-session", "turn-1"))

    def test_stop_orchestrator_rejects_stale_verdict_without_turn_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict_path = Path(tmp) / "mimo_last_verdict.json"
            verdict_path.write_text(json.dumps({
                "orch-session": {
                    "turn_id": "old-turn",
                    "verdict": "PASS",
                    "updated_at": time.time() - 121,
                }
            }))
            with (
                mock.patch.object(stop_orchestrator, "MIMO_VERDICT_PATH", verdict_path),
                mock.patch.object(stop_orchestrator, "log"),
            ):
                self.assertFalse(stop_orchestrator.wait_for_mimo_pass("orch-session", ""))

    def test_aaa_milestone_gate_blocks_on_project_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                mock.patch.object(stop_orchestrator, "run_project_aaa_checks", return_value=["pytest failed"]),
                mock.patch.object(stop_orchestrator, "log"),
                redirect_stdout(stdout),
            ):
                with self.assertRaises(SystemExit) as raised:
                    stop_orchestrator.run_aaa_gate(Path(tmp), "ready to merge AAA milestone")

        data = json.loads(stdout.getvalue())
        self.assertEqual(int(raised.exception.code or 0), 0)
        self.assertEqual(data["decision"], "block")
        self.assertIn("[AAA Milestone Gate]", data["reason"])
        self.assertIn("pytest failed", data["reason"])

    def test_auto_pr_commits_with_mimo_only_auditor_when_claude_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            (repo / "README.md").write_text("initial\n")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("changed\n")
            handoff: dict = {}

            with (
                mock.patch.object(stop_orchestrator, "AUTO_PR_ENABLED", True),
                mock.patch.object(stop_orchestrator, "CLAUDE_AUDIT_ENABLED", False),
                mock.patch.object(stop_orchestrator, "github_pr_preflight", return_value=(True, "ok")),
                mock.patch.object(stop_orchestrator, "push", return_value=True),
                mock.patch.object(stop_orchestrator, "open_pr", return_value="https://example.test/pr/1"),
                mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
                mock.patch.object(stop_orchestrator, "write_handoff", side_effect=lambda data: handoff.update(data)),
                mock.patch.object(stop_orchestrator, "log"),
            ):
                branch, sha, pr_url, status = stop_orchestrator.commit_push_pr(
                    "session-auto-pr",
                    repo,
                    "Summary line\nMore detail",
                )
            message = git(repo, "log", "-1", "--pretty=%B").stdout

        self.assertEqual(branch, "codex/session-/task-1")
        self.assertTrue(sha)
        self.assertEqual(pr_url, "https://example.test/pr/1")
        self.assertEqual(status, "created")
        self.assertIn("Audited-by: MiMo-v2.5-Pro", message)
        self.assertNotIn("Claude", message)
        self.assertEqual(handoff["last_task"]["status"], "created")
        self.assertEqual(handoff["last_task"]["sha"], sha)

    def test_auto_pr_preflight_failure_skips_commit_and_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            (repo / "README.md").write_text("initial\n")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            before = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
            (repo / "README.md").write_text("changed\n")
            handoff: dict = {}

            with (
                mock.patch.object(stop_orchestrator, "AUTO_PR_ENABLED", True),
                mock.patch.object(stop_orchestrator, "github_pr_preflight", return_value=(False, "gh auth unavailable")),
                mock.patch.object(stop_orchestrator, "push") as push,
                mock.patch.object(stop_orchestrator, "open_pr") as open_pr,
                mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
                mock.patch.object(stop_orchestrator, "write_handoff", side_effect=lambda data: handoff.update(data)),
                mock.patch.object(stop_orchestrator, "log"),
            ):
                branch, sha, pr_url, status = stop_orchestrator.commit_push_pr(
                    "session-auto-pr",
                    repo,
                    "Summary line\nMore detail",
                )
            after = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

        self.assertEqual((branch, sha, pr_url, status), ("", "", "", "preflight_failed"))
        self.assertEqual(after, before)
        self.assertEqual(handoff, {})
        push.assert_not_called()
        open_pr.assert_not_called()

    def test_auto_pr_push_recovers_after_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            (repo / "README.md").write_text("initial\n")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("changed\n")
            handoff: dict = {}

            with (
                mock.patch.object(stop_orchestrator, "AUTO_PR_ENABLED", True),
                mock.patch.object(stop_orchestrator, "CLAUDE_AUDIT_ENABLED", False),
                mock.patch.object(stop_orchestrator, "AUTO_PR_RETRY_DELAY_S", 0),
                mock.patch.object(stop_orchestrator, "github_pr_preflight", return_value=(True, "ok")),
                mock.patch.object(stop_orchestrator, "push", side_effect=[False, True]) as push,
                mock.patch.object(stop_orchestrator, "open_pr", return_value="https://example.test/pr/2"),
                mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
                mock.patch.object(stop_orchestrator, "write_handoff", side_effect=lambda data: handoff.update(data)),
                mock.patch.object(stop_orchestrator, "log"),
            ):
                branch, sha, pr_url, status = stop_orchestrator.commit_push_pr(
                    "session-auto-pr",
                    repo,
                    "Summary line\nMore detail",
                )

        self.assertEqual(status, "created")
        self.assertEqual(pr_url, "https://example.test/pr/2")
        self.assertTrue(sha)
        self.assertEqual(push.call_count, 2)
        self.assertEqual(handoff["last_task"]["status"], "created")

    def test_auto_pr_pr_failure_records_partial_and_blocks_chaining(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            (repo / "README.md").write_text("initial\n")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("changed\n")
            handoff: dict = {}

            with (
                mock.patch.object(stop_orchestrator, "AUTO_PR_ENABLED", True),
                mock.patch.object(stop_orchestrator, "AUTO_PR_RETRY_DELAY_S", 0),
                mock.patch.object(stop_orchestrator, "github_pr_preflight", return_value=(True, "ok")),
                mock.patch.object(stop_orchestrator, "push", return_value=True),
                mock.patch.object(stop_orchestrator, "open_pr", return_value="") as open_pr,
                mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
                mock.patch.object(stop_orchestrator, "write_handoff", side_effect=lambda data: handoff.update(data)),
                mock.patch.object(stop_orchestrator, "log"),
            ):
                branch, sha, pr_url, status = stop_orchestrator.commit_push_pr(
                    "session-auto-pr",
                    repo,
                    "Summary line\nMore detail",
                )

        self.assertEqual(status, "pr_failed")
        self.assertEqual(pr_url, "")
        self.assertTrue(branch)
        self.assertTrue(sha)
        self.assertEqual(open_pr.call_count, 3)
        self.assertEqual(handoff["last_task"]["status"], "pr_failed")
        self.assertEqual(handoff["last_task"]["sha"], sha)

    def test_stop_main_blocks_after_partial_auto_pr_and_does_not_chain(self) -> None:
        stdout = io.StringIO()
        payload = {
            "session_id": "session-auto-pr",
            "turn_id": "turn-1",
            "cwd": "/tmp",
            "last_assistant_message": "Completed task. " * 40,
        }
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            redirect_stdout(stdout),
            mock.patch.object(stop_orchestrator, "audit_with_claude", return_value=True),
            mock.patch.object(stop_orchestrator, "is_git_repo", return_value=False),
            mock.patch.object(stop_orchestrator, "wait_for_mimo_pass", return_value=True),
            mock.patch.object(stop_orchestrator, "commit_push_pr", return_value=("branch", "abc123", "", "pr_failed")),
            mock.patch.object(stop_orchestrator, "next_task") as next_task,
            mock.patch.object(stop_orchestrator, "log"),
        ):
            with self.assertRaises(SystemExit) as raised:
                stop_orchestrator.main()

        data = json.loads(stdout.getvalue())
        self.assertEqual(int(raised.exception.code or 0), 0)
        self.assertEqual(data["decision"], "block")
        self.assertIn("[Auto-PR incomplete]", data["reason"])
        self.assertIn("pr_failed", data["reason"])
        next_task.assert_not_called()

    def test_github_pr_preflight_requires_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)

            ok, reason = stop_orchestrator.github_pr_preflight(repo)

        self.assertFalse(ok)
        self.assertEqual(reason, "missing git origin remote")

    def test_self_spec_uses_queued_backlog_and_pops_item(self) -> None:
        handoff = {
            "tasks_completed_count": 0,
            "queued_backlog": [
                {"description": "Add coverage for parser", "source_file": "agent/parser.py", "priority": 70},
                {"description": "Resolve TODO", "source_file": "agent/todo.py", "priority": 50},
            ],
        }
        written: dict = {}
        with (
            mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
            mock.patch.object(stop_orchestrator, "write_handoff", side_effect=lambda data: written.update(data)),
        ):
            task = stop_orchestrator.next_task("session-self-spec", Path("/tmp/repo"), "branch", "pr")

        self.assertIn("Add coverage for parser", task)
        self.assertIn("agent/parser.py", task)
        self.assertEqual([item["description"] for item in written["queued_backlog"]], ["Resolve TODO"])


if __name__ == "__main__":
    unittest.main()

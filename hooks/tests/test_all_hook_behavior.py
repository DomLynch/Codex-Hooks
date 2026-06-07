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
import autonomy_daemon  # noqa: E402
from lib import autonomy  # noqa: E402
from lib import api  # noqa: E402


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
        self.assertIn("General Work Quality Contract", context)
        self.assertIn("Context Efficiency Contract", context)
        self.assertIn("smallest surgical change", context)
        self.assertIn("minimal LOC", context)
        self.assertIn("capped output", context)
        self.assertIn("Avoid duplicate parallel exploration", context)
        self.assertIn("compact context packet", context)
        self.assertIn("Mandatory: use Codegraph MCP first", context)
        self.assertIn("two-pass review", context)
        self.assertIn("reproduce or cite the concrete failure", context)
        self.assertIn("Do not ask for confirmation", context)
        self.assertIn("reasonable reversible default", context)
        self.assertNotIn("cheapest relevant verification", context)

    def test_user_prompt_submit_injects_active_lessons(self) -> None:
        with (
            mock.patch.object(user_prompt_submit, "active_lessons", return_value=["Run the negative path test."]),
            mock.patch.object(user_prompt_submit, "log"),
        ):
            _, out = run_main_with_stdin(
                user_prompt_submit.main,
                {"prompt": "fix parser.py", "cwd": "/tmp/project"},
            )

        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Active Lessons", context)
        self.assertIn("Run the negative path test.", context)

    def test_call_mimo_uses_direct_mimo_http(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps({"choices": [{"message": {"content": "VERDICT: PASS"}}]}).encode()

        with (
            mock.patch.object(api, "load_api_key", return_value="mimo-key") as load_key,
            mock.patch.object(api.urllib.request, "urlopen", return_value=Response()) as urlopen,
        ):
            result = api.call_mimo("audit this", timeout=7)

        self.assertEqual(result, "VERDICT: PASS")
        load_key.assert_called_once()
        urlopen.assert_called_once()

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

    def test_mimo_audit_prompt_blocks_bloat_and_random_debugging(self) -> None:
        self.assertIn("file/module sprawl", mimo_audit.AUDIT_PROMPT)
        self.assertIn("speculative abstractions", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Hardcoded or silo-specific fixes", mimo_audit.AUDIT_PROMPT)
        self.assertIn("mandatory Codegraph MCP first", mimo_audit.AUDIT_PROMPT)
        self.assertIn("two-pass review", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Random debugging", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Audit-only or review-only requests", mimo_audit.AUDIT_PROMPT)

    def test_mid_run_prompts_check_codegraph_routing(self) -> None:
        self.assertIn("skipped Codegraph MCP", post_tool.MIMO_CHECKPOINT_PROMPT)
        self.assertIn("Codegraph/MCP routing", Path(stop_orchestrator.__file__).read_text())

    def test_mimo_audit_prompt_blocks_metadata_only_false_alarms(self) -> None:
        self.assertIn('what counts as "task content"', mimo_audit.AUDIT_PROMPT)
        self.assertIn("metadata only", mimo_audit.AUDIT_PROMPT)
        self.assertIn("unable to audit", mimo_audit.AUDIT_PROMPT)
        self.assertIn("audit THAT content on its merits", mimo_audit.AUDIT_PROMPT)
        self.assertIn("pasted evidence", mimo_audit.AUDIT_PROMPT)
        self.assertIn("phantom truncation", mimo_audit.AUDIT_PROMPT)

    def test_mimo_bare_verdicts_are_normalized(self) -> None:
        self.assertEqual(mimo_audit.normalize_verdict("PASS"), "VERDICT: PASS")
        self.assertEqual(
            mimo_audit.normalize_verdict("issues\n1. Missing regression test"),
            "VERDICT: ISSUES\n1. Missing regression test",
        )

    def test_mimo_audit_uses_direct_mimo_http(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps({"choices": [{"message": {"content": "PASS"}}]}).encode()

        with mock.patch.object(mimo_audit.urllib.request, "urlopen", return_value=Response()) as urlopen:
            verdict = mimo_audit.ask_mimo("direct-api-key", "USER:\nfix code", max_tokens=20)

        self.assertEqual(verdict, "VERDICT: PASS")
        urlopen.assert_called_once()

    def test_mimo_retry_budget_stays_under_stop_timeout(self) -> None:
        retry_budget = (
            mimo_audit.AUDIT_TIMEOUT_S * mimo_audit.AUDIT_MAX_ATTEMPTS
            + mimo_audit.AUDIT_RETRY_DELAY_S * (mimo_audit.AUDIT_MAX_ATTEMPTS - 1)
        )
        self.assertLess(retry_budget, 90)

    def test_mimo_bare_pass_allows_stop_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "mimo-bare-pass",
                "turn_id": "turn-1",
                "last_assistant_message": "Completed task. " * 40,
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "load_api_key", return_value="test-key"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="PASS"),
                mock.patch.object(mimo_audit, "repair_verdict_format") as repair,
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        repair.assert_not_called()
        self.assertEqual(verdict["mimo-bare-pass"]["verdict"], "PASS")

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

    def test_stop_orchestrator_waits_past_stale_same_turn_verdict(self) -> None:
        now = time.time()
        states = [
            {"orch-session": {"turn_id": "turn-1", "verdict": "ISSUES", "updated_at": now - 10}},
            {"orch-session": {"turn_id": "turn-1", "verdict": "PASS", "updated_at": now + 1}},
        ]

        def load_state(*_args, **_kwargs):
            return states.pop(0) if len(states) > 1 else states[0]

        with (
            mock.patch.object(stop_orchestrator, "MIMO_WAIT_TIMEOUT_S", 2),
            mock.patch.object(stop_orchestrator, "load_json_locked", side_effect=load_state),
            mock.patch.object(stop_orchestrator.time, "sleep"),
            mock.patch.object(stop_orchestrator, "log"),
        ):
            self.assertTrue(stop_orchestrator.wait_for_mimo_pass("orch-session", "turn-1"))

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

    def test_shared_autonomy_queue_dedupes_and_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue.json"
            stop = Path(tmp) / "STOP"
            with (
                mock.patch.object(autonomy, "QUEUE_PATH", queue),
                mock.patch.object(autonomy, "STOP_PATH", stop),
                mock.patch.object(autonomy, "SECRETS_ENV", Path(tmp) / "missing_env"),
            ):
                added = autonomy.enqueue_tasks(
                    [
                        {"title": "Fix parser", "body": "Add edge test", "cwd": tmp, "priority": 70},
                        {"title": "Fix parser", "body": "Add edge test", "cwd": tmp, "priority": 70},
                    ],
                    "test",
                )
                first = autonomy.pop_next_task("codex", Path(tmp))
                second = autonomy.pop_next_task("claude", Path(tmp))

        self.assertEqual(added, 1)
        self.assertEqual(first["title"], "Fix parser")
        self.assertEqual(first["status"], "leased")
        self.assertIsNone(second)

    def test_shared_autonomy_queue_holds_risky_tasks_until_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue.json"
            with (
                mock.patch.object(autonomy, "QUEUE_PATH", queue),
                mock.patch.object(autonomy, "STOP_PATH", Path(tmp) / "STOP"),
                mock.patch.object(autonomy, "SECRETS_ENV", Path(tmp) / "missing_env"),
            ):
                added = autonomy.enqueue_tasks(
                    [{
                        "title": "Deploy production cleanup",
                        "body": "SSH to the VPS and remove stale files after deploy.",
                        "cwd": tmp,
                        "priority": 90,
                    }],
                    "test",
                )
                held = json.loads(queue.read_text())["tasks"][0]
                blocked = autonomy.pop_next_task("codex", Path(tmp))

                def approve(state: dict) -> dict:
                    state["tasks"][0]["status"] = "queued"
                    state["tasks"][0]["approved"] = True
                    state["tasks"][0].pop("hold_reason", None)
                    return state

                autonomy.update_json_locked(queue, approve)
                approved = autonomy.pop_next_task("codex", Path(tmp))

        self.assertEqual(added, 1)
        self.assertEqual(held["status"], "held")
        self.assertIn("risky autonomous task", held["hold_reason"])
        self.assertIsNone(blocked)
        self.assertEqual(approved["status"], "leased")
        self.assertTrue(approved["approved"])

    def test_active_lessons_expire_and_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_800_000_000.0
            lessons_state = Path(tmp) / "lessons_active.json"
            lessons_jsonl = Path(tmp) / "lessons.jsonl"
            lessons_md = Path(tmp) / "lessons_active.md"
            with (
                mock.patch.object(autonomy, "LESSONS_STATE", lessons_state),
                mock.patch.object(autonomy, "LESSONS_JSONL", lessons_jsonl),
                mock.patch.object(autonomy, "LESSONS_ACTIVE", lessons_md),
                mock.patch.object(autonomy, "SECRETS_ENV", Path(tmp) / "missing_env"),
                mock.patch.object(autonomy.time, "time", return_value=now),
            ):
                for idx in range(7):
                    autonomy.record_lesson("test", f"Lesson {idx}")
                self.assertEqual(
                    autonomy.active_lessons(limit=10),
                    ["Lesson 6", "Lesson 5", "Lesson 4", "Lesson 3", "Lesson 2"],
                )
                lessons_state.write_text(json.dumps({
                    "updated_at": now,
                    "lessons": [
                        {"lesson": "expired lesson", "source": "test", "ts": now - 15 * 24 * 60 * 60},
                        {"lesson": "fresh lesson", "source": "test", "ts": now - 13 * 24 * 60 * 60},
                    ],
                }))
                self.assertEqual(autonomy.active_lessons(limit=10), ["fresh lesson"])

    def test_autonomy_daemon_installer_has_lifecycle_and_stop_switch(self) -> None:
        script = (HOOKS_DIR.parents[0] / "scripts" / "install_autonomy_daemon.sh").read_text()

        self.assertIn("<key>StartInterval</key>", script)
        self.assertIn("<integer>720</integer>", script)
        self.assertIn("launchctl bootstrap", script)
        self.assertIn("AUTONOMY_STOP", script)

    def test_stop_next_task_prefers_shared_queue(self) -> None:
        with mock.patch.object(
            stop_orchestrator,
            "pop_next_task",
            return_value={"title": "Queued fix", "body": "Patch app.py and run tests.", "source_file": "app.py"},
        ):
            task = stop_orchestrator.next_task("s1", Path("/tmp/repo"), "", "")

        self.assertIn("Queued fix", task)
        self.assertIn("Patch app.py", task)
        self.assertIn("Source: app.py", task)

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

    def test_milestone_detector_ignores_grade_and_hook_feedback(self) -> None:
        hook_feedback = (
            '<hook_prompt hook_run_id="stop:4:/Users/domininclynch/.codex/config.toml">'
            "[AAA Milestone Gate]\n\nnpm test timed out"
            "</hook_prompt>"
        )

        self.assertFalse(stop_orchestrator.milestone_detected("Functional verdict: AAA now."))
        self.assertFalse(stop_orchestrator.milestone_detected(hook_feedback))
        self.assertTrue(stop_orchestrator.milestone_detected("ready to merge after final checks"))
        self.assertTrue(stop_orchestrator.milestone_detected("this is an AAA milestone release"))
        self.assertTrue(stop_orchestrator.milestone_detected("ship it now"))

    def test_milestone_detector_ignores_historical_handover_terms(self) -> None:
        handover = """
        USER:
        Local setup handover:
        - provider model mimo-v2.5-pro
        - queried upstream /v1/models
        - previous release cleanup notes are historical context
        - prior memory mentions AAA Milestone Gate false trigger
        """

        self.assertFalse(stop_orchestrator.milestone_detected(handover))

    def test_project_aaa_checks_skip_package_tests_when_worktree_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text(json.dumps({"scripts": {"test": "node slow-suite.js"}}))

            with (
                mock.patch.object(stop_orchestrator, "safe_run") as safe_run,
                mock.patch.object(stop_orchestrator, "log"),
            ):
                failures = stop_orchestrator.run_project_aaa_checks(repo, [])

        self.assertEqual(failures, [])
        safe_run.assert_not_called()

    def test_project_aaa_checks_use_declared_pnpm_changed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            changed = repo / "src" / "app.ts"
            changed.write_text("export const ok = true;\n")
            (repo / "package.json").write_text(
                json.dumps({
                    "name": "demo",
                    "packageManager": "pnpm@10.33.2",
                    "scripts": {"test": "node slow-suite.js", "test:changed": "vitest related --run"},
                })
            )

            with (
                mock.patch.object(stop_orchestrator.shutil, "which", side_effect=lambda name: f"/bin/{name}" if name == "pnpm" else None),
                mock.patch.object(stop_orchestrator, "safe_run", return_value=(0, "", "")) as safe_run,
            ):
                failures = stop_orchestrator.run_project_aaa_checks(repo, [changed])

        self.assertEqual(failures, [])
        safe_run.assert_called_once_with(["pnpm", "run", "test:changed"], cwd=repo, timeout=120)

    def test_project_aaa_checks_runs_package_test_when_changed_gate_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            changed = repo / "src" / "index.ts"
            changed.write_text("export const ok = true;\n")
            (repo / "package.json").write_text(
                json.dumps({
                    "name": "demo",
                    "packageManager": "pnpm@10.33.2",
                    "scripts": {"test": "node scripts/test-projects.mjs"},
                })
            )

            with (
                mock.patch.object(stop_orchestrator.shutil, "which", return_value="/bin/tool"),
                mock.patch.object(stop_orchestrator, "safe_run", return_value=(0, "", "")) as safe_run,
            ):
                failures = stop_orchestrator.run_project_aaa_checks(repo, [changed])

        self.assertEqual(failures, [])
        safe_run.assert_called_once_with(["pnpm", "test", "--", "--watch=false"], cwd=repo, timeout=120)

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
        self.assertEqual(handoff["cwd"], str(repo))
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
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "agent").mkdir()
            (repo / "agent/parser.py").write_text("# TODO: add coverage\n")
            (repo / "agent/todo.py").write_text("# FIXME: resolve\n")
            handoff = {
                "cwd": str(repo),
                "tasks_completed_count": 0,
                "queued_backlog": [
                    {"description": "Add coverage for parser", "source_file": "agent/parser.py:1", "priority": 70},
                    {"description": "Resolve TODO", "source_file": "agent/todo.py:1", "priority": 50},
                ],
            }
            written: dict = {}
            with (
                mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
                mock.patch.object(stop_orchestrator, "write_handoff", side_effect=lambda data: written.update(data)),
            ):
                task = stop_orchestrator.next_task("session-self-spec", repo, "branch", "pr")

        self.assertIn("Add coverage for parser", task)
        self.assertIn("agent/parser.py", task)
        self.assertEqual([item["description"] for item in written["queued_backlog"]], ["Resolve TODO"])

    def test_self_spec_ignores_stale_handoff_backlog_from_other_cwd(self) -> None:
        handoff = {
            "cwd": "/tmp/old-repo",
            "tasks_completed_count": 0,
            "queued_backlog": [
                {"description": "Fill incomplete markdown section", "source_file": "missing.md", "priority": 30},
            ],
        }
        with (
            mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
            mock.patch.object(stop_orchestrator, "write_handoff") as write_handoff,
            mock.patch.object(stop_orchestrator, "is_git_repo", return_value=False),
            mock.patch.object(stop_orchestrator, "call_mimo", return_value="STATUS: COMPLETE"),
            mock.patch.object(stop_orchestrator, "log"),
        ):
            task = stop_orchestrator.next_task("session-self-spec", Path("/tmp/current-repo"), "branch", "pr")

        self.assertEqual(task, "")
        write_handoff.assert_not_called()

    def test_self_spec_filters_missing_handoff_backlog_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoff = {
                "cwd": str(repo),
                "tasks_completed_count": 0,
                "queued_backlog": [
                    {
                        "description": "Resolve stale phase two diff item",
                        "source_file": "phase2_workspace_diff.md:1719",
                        "priority": 30,
                    },
                ],
            }
            written: dict = {}
            with (
                mock.patch.object(stop_orchestrator, "load_handoff", return_value=handoff),
                mock.patch.object(stop_orchestrator, "write_handoff", side_effect=lambda data: written.update(data)),
                mock.patch.object(stop_orchestrator, "is_git_repo", return_value=False),
                mock.patch.object(stop_orchestrator, "call_mimo", return_value="STATUS: COMPLETE"),
                mock.patch.object(stop_orchestrator, "log"),
            ):
                task = stop_orchestrator.next_task("session-self-spec", repo, "branch", "pr")

        self.assertEqual(task, "")
        self.assertEqual(written["queued_backlog"], [])

    def test_autonomy_daemon_mines_local_tasks_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            path = repo / "app.py"
            path.write_text("# TODO: add coverage\n")
            tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertEqual(tasks[0]["title"], "Resolve backlog marker in app.py")
        self.assertEqual(tasks[0]["cwd"], str(repo))

    def test_autonomy_daemon_mines_local_tasks_without_rg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            path = repo / "app.py"
            path.write_text("# TODO: add coverage\n")
            with mock.patch.object(autonomy_daemon.shutil, "which", return_value=None):
                tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertTrue(tasks)
        self.assertEqual(tasks[0]["title"], "Resolve backlog marker in app.py")
        self.assertEqual(tasks[0]["cwd"], str(repo))

    def test_autonomy_daemon_falls_back_when_rg_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            path = repo / "app.py"
            path.write_text("# XXX: tighten scanner\n")
            with (
                mock.patch.object(autonomy_daemon.shutil, "which", return_value="/usr/bin/rg"),
                mock.patch.object(autonomy_daemon, "safe_run", return_value=(0, "", "")),
            ):
                tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertTrue(tasks)
        self.assertEqual(tasks[0]["title"], "Resolve backlog marker in app.py")
        self.assertEqual(tasks[0]["cwd"], str(repo))

    def test_autonomy_daemon_ignores_marker_config_and_prose_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            (repo / "autonomy_daemon.py").write_text('BACKLOG_MARKER_RE = re.compile(r"TODO|FIXME|XXX")\n')
            (repo / "notes.md").write_text("Mention TODO/FIXME/XXX without assigning work.\n")
            tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertEqual(tasks, [])

    def test_autonomy_daemon_accepts_owner_marker_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            path = repo / "app.py"
            path.write_text("# TODO(owner): add coverage\n")
            tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertEqual(tasks[0]["title"], "Resolve backlog marker in app.py")


if __name__ == "__main__":
    unittest.main()

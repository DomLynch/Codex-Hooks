from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib.claude_adapter import (  # noqa: E402
    changed_files_in_claude_post_tool,
    extract_last_assistant_message,
    latest_task_slice_claude,
    normalize_payload,
    synthesize_turn_id,
)
import mimo_audit  # noqa: E402
import autonomy_daemon  # noqa: E402
import post_tool  # noqa: E402
import session_start  # noqa: E402
import stop_orchestrator  # noqa: E402
import user_prompt_submit  # noqa: E402
from lib import autonomy  # noqa: E402
from lib import api, state  # noqa: E402
from lib.transcript import latest_task_slice as latest_codex_style_task_slice  # noqa: E402


def run_main_with_stdin(fn, payload: dict) -> tuple[int | None, str]:
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    with mock.patch("sys.stdin", stdin), redirect_stdout(stdout):
        try:
            fn()
        except SystemExit as exc:
            return int(exc.code or 0), stdout.getvalue()
    return None, stdout.getvalue()


class ClaudeAdapterTests(unittest.TestCase):
    def _transcript(self, entries: list[dict]) -> str:
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl")
        with tmp:
            for entry in entries:
                tmp.write(json.dumps(entry) + "\n")
        return tmp.name

    def test_extract_last_assistant_message_from_text_blocks(self) -> None:
        path = self._transcript([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "older"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "latest"}]}},
        ])
        self.assertEqual(extract_last_assistant_message(path), "latest")

    def test_latest_task_slice_starts_at_last_user_message(self) -> None:
        path = self._transcript([
            {"type": "user", "message": {"content": "old task"}},
            {"type": "assistant", "message": {"content": "old answer"}},
            {"type": "user", "message": {"content": "fix app.py"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "working"},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "app.py"}},
                    ]
                },
            },
        ])
        text = latest_task_slice_claude(path)
        self.assertIn("fix app.py", text)
        self.assertIn("TOOL CALL: Edit", text)
        self.assertNotIn("old task", text)

    def test_codex_style_transcript_helper_hard_caps_huge_first_user_entry(self) -> None:
        path = self._transcript([
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": "A" * 200_000}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ])

        text = latest_codex_style_task_slice(path)

        self.assertLessEqual(len(text), 4000)
        self.assertIn("[head truncated]", text)
        self.assertIn("older task details omitted", text)

    def test_normalize_payload_adds_turn_and_last_message(self) -> None:
        path = self._transcript([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        ])
        payload = normalize_payload({"session_id": "s1", "transcript_path": path, "cwd": "/tmp"})
        self.assertEqual(payload["last_assistant_message"], "done")
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["cwd"], "/tmp")
        self.assertTrue(payload["turn_id"])

    def test_turn_id_changes_for_retry_state(self) -> None:
        path = self._transcript([{"type": "user", "message": {"content": "x"}}])
        first = synthesize_turn_id("s1", path, False)
        retry = synthesize_turn_id("s1", path, True)
        self.assertNotEqual(first, retry)

    def test_changed_files_from_claude_write_payload(self) -> None:
        payload = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/demo.py"}}
        self.assertEqual(changed_files_in_claude_post_tool(payload), [Path("/tmp/demo.py")])

    def test_short_tool_activity_should_not_skip_audit(self) -> None:
        self.assertTrue(mimo_audit.has_tool_activity("TOOL CALL:\nEdit: app.py"))
        self.assertFalse(mimo_audit.has_tool_activity("ok thanks"))

    def test_mimo_prompt_blocks_metadata_only_false_alarms(self) -> None:
        self.assertIn('what counts as "task content"', mimo_audit.AUDIT_PROMPT)
        self.assertIn("metadata only", mimo_audit.AUDIT_PROMPT)
        self.assertIn("unable to audit", mimo_audit.AUDIT_PROMPT)
        self.assertIn("audit THAT content on its merits", mimo_audit.AUDIT_PROMPT)
        self.assertIn("pasted evidence", mimo_audit.AUDIT_PROMPT)
        self.assertIn("phantom truncation", mimo_audit.AUDIT_PROMPT)

    def test_mimo_prompt_blocks_bloat_and_random_debugging(self) -> None:
        self.assertIn("file/module sprawl", mimo_audit.AUDIT_PROMPT)
        self.assertIn("speculative abstractions", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Hardcoded or silo-specific fixes", mimo_audit.AUDIT_PROMPT)
        self.assertIn("three materially different", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Semble searches before edits", mimo_audit.AUDIT_PROMPT)
        self.assertIn("together rather than as alternatives", mimo_audit.AUDIT_PROMPT)
        self.assertIn("concise coverage receipt", mimo_audit.AUDIT_PROMPT)
        self.assertIn("two-pass review", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Random debugging", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Audit-only or review-only requests", mimo_audit.AUDIT_PROMPT)

    def test_claude_mimo_prompt_calibrates_status_checks(self) -> None:
        self.assertIn("Status/check-only calibration", mimo_audit.AUDIT_PROMPT)
        self.assertIn("direct command/log/state output", mimo_audit.AUDIT_PROMPT)
        self.assertIn("source/live parity", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Do not demand fresh checks merely", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Public receipt calibration", mimo_audit.AUDIT_PROMPT)
        self.assertIn("public publish counts", mimo_audit.AUDIT_PROMPT)
        self.assertIn("public record fields such as `agentId`", mimo_audit.AUDIT_PROMPT)
        self.assertIn("not proven", mimo_audit.AUDIT_PROMPT)
        self.assertIn("internal pipeline success alone", mimo_audit.AUDIT_PROMPT)

    def test_claude_mimo_prompt_constrains_executable_demands(self) -> None:
        self.assertIn("Conversation turns are immutable", mimo_audit.AUDIT_PROMPT)
        self.assertIn("audit-only/review-only mode", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Tool output (stdout/stderr blocks", mimo_audit.AUDIT_PROMPT)
        self.assertIn("MECHANICALLY TRUNCATED", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Quality Floor", mimo_audit.AUDIT_PROMPT)
        self.assertIn("py_compile/ruff", mimo_audit.AUDIT_PROMPT)
        self.assertIn("NEVER flag an edit", mimo_audit.AUDIT_PROMPT)
        self.assertIn("truncated/incomplete/corrupting", mimo_audit.AUDIT_PROMPT)

    def test_claude_task_slice_marks_tool_arg_elision(self) -> None:
        path = self._transcript([
            {"type": "user", "message": {"content": "edit app.py"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"new_string": "A" * 10_000},
                        },
                        {"type": "text", "text": "quality files=1 ok=True"},
                    ]
                },
            },
        ])

        text = latest_task_slice_claude(path)

        self.assertIn("slice-elided by compactor", text)
        self.assertNotIn("...[omitted]...", text)
        self.assertIn("quality files=1 ok=True", text)

    def test_claude_task_slice_preserves_middle_verification_receipt(self) -> None:
        path = self._transcript([
            {"type": "user", "message": {"content": "verify and deploy " + "U" * 5000}},
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "31 tests passed\nAll checks passed!"}]},
            },
            {"type": "assistant", "message": {"content": "Final verified summary. " + "A" * 5000}},
        ])

        text = latest_task_slice_claude(path)

        self.assertLessEqual(len(text), 4000)
        self.assertIn("verify and deploy", text)
        self.assertIn("verification receipts preserved", text)
        self.assertIn("31 tests passed", text)
        self.assertIn("Final verified summary", text)

    def test_native_receipt_keeps_tail_and_original_request_after_feedback(self) -> None:
        path = self._transcript([
            {"type": "user", "message": {"content": "Check tests, no edits"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "receipt-1", "name": "Bash", "input": {"command": "pytest -q"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "receipt-1", "content": [
                    {"type": "text", "text": "x" * 5000 + "\nFAILED test_boundary; exit_code=1"},
                ]},
            ]}},
            {"type": "user", "message": {"content": "<hook_prompt>Show evidence</hook_prompt>"}},
            {"type": "assistant", "message": {"content": "The boundary test failed."}},
        ])
        text = latest_task_slice_claude(path)
        for receipt in ("Check tests, no edits", "pytest -q", "receipt-1",
                        "FAILED test_boundary; exit_code=1", "PRIOR REVIEW"):
            self.assertIn(receipt, text)
        self.assertLessEqual(len(text), 4000)

    def test_mid_run_prompts_check_semble_and_codegraph_routing(self) -> None:
        self.assertIn("three-query Semble discovery", post_tool.MIMO_CHECKPOINT_PROMPT)
        self.assertIn("Codegraph structural/impact analysis", post_tool.MIMO_CHECKPOINT_PROMPT)
        self.assertIn("Semble discovery plus Codegraph", Path(stop_orchestrator.__file__).read_text())

    def test_claude_mid_run_audit_is_sparse_and_not_a_completion_gate(self) -> None:
        self.assertEqual(post_tool.MIMO_AUDIT_EDIT_THRESHOLD, 15)
        self.assertIn("This is an in-progress checkpoint", post_tool.MIMO_CHECKPOINT_PROMPT)
        self.assertIn("Do NOT block merely", post_tool.MIMO_CHECKPOINT_PROMPT)

    def test_claude_checkpoint_branch_failure_state_clears_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            with mock.patch.object(state, "CHECKPOINT_PATH", path):
                state.update_checkpoint_state("session", edits=5, branch_failed=True)
                failed = state.checkpoint_state("session")
                state.update_checkpoint_state("session", committed=True)
                committed = state.checkpoint_state("session")

        self.assertIn("last_branch_failure_at", failed)
        self.assertNotIn("last_branch_failure_at", committed)
        self.assertEqual(committed["edits_since_last_commit"], 0)

    def test_user_prompt_contract_requires_mcp_minimal_loc_and_review(self) -> None:
        context = user_prompt_submit.context_for("fix hook code", Path("/tmp/project"))
        self.assertIn("minimal LOC", context)
        self.assertIn("Context Efficiency Contract", context)
        self.assertIn("capped output", context)
        self.assertIn("Avoid duplicate parallel exploration", context)
        self.assertIn("compact context packet", context)
        self.assertIn("three materially different Semble searches", context)
        self.assertIn("together, not as alternatives", context)
        self.assertIn("Codegraph MCP for structural context/impact", context)
        self.assertIn("only then degrade to Semble + `rg`", context)
        self.assertIn("concise coverage receipt", context)
        self.assertIn("two-pass review", context)

    def test_claude_session_baseline_includes_context_efficiency(self) -> None:
        self.assertIn("capped output", session_start.SESSION_BASELINE)
        self.assertIn("Avoid duplicate parallel exploration", session_start.SESSION_BASELINE)
        self.assertIn("compact context packet", session_start.SESSION_BASELINE)
        self.assertIn("three materially different Semble searches", session_start.SESSION_BASELINE)
        self.assertIn("together, not as alternatives", session_start.SESSION_BASELINE)
        self.assertIn("only then degrade to Semble + `rg`", session_start.SESSION_BASELINE)
        self.assertIn("concise coverage receipt", session_start.SESSION_BASELINE)

    def test_mimo_bare_verdicts_are_normalized(self) -> None:
        self.assertEqual(mimo_audit.normalize_verdict("PASS"), "VERDICT: PASS")
        self.assertEqual(
            mimo_audit.normalize_verdict("issues\n1. Missing regression test"),
            "VERDICT: ISSUES\n1. Missing regression test",
        )
        self.assertFalse(mimo_audit.valid_verdict("VERDICT: ISSUES"))
        self.assertTrue(mimo_audit.valid_verdict("VERDICT: ISSUES\n1. Missing regression test"))

    def test_claude_bare_issues_repair_failure_is_format_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "claude-bare-issues",
                "turn_id": "turn-1",
                "last_assistant_message": "Completed implementation. " * 40,
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: ISSUES"),
                mock.patch.object(mimo_audit, "repair_verdict_format", return_value="VERDICT: ISSUES"),
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertEqual(verdict["claude-bare-issues"]["verdict"], "ERROR")

    def test_mimo_audit_uses_shared_audit_transport(self) -> None:
        with mock.patch.object(mimo_audit, "call_mimo", return_value="PASS") as call_mimo:
            verdict = mimo_audit.ask_mimo("USER:\nfix code", max_tokens=20)

        self.assertEqual(verdict, "VERDICT: PASS")
        self.assertEqual(call_mimo.call_args.kwargs["lane"], "stop")
        self.assertEqual(call_mimo.call_args.kwargs["max_tokens"], 20)

    def test_claude_mimo_retry_budget_stays_under_stop_timeout(self) -> None:
        self.assertLess(mimo_audit.AUDIT_RETRY_BUDGET_S, mimo_audit.AUDIT_HOOK_TIMEOUT_S)
        self.assertGreater(stop_orchestrator.MIMO_WAIT_TIMEOUT_S, mimo_audit.AUDIT_RETRY_BUDGET_S)

    def test_claude_mimo_connection_reset_retries_once_then_passes(self) -> None:
        with (
            mock.patch.object(mimo_audit, "AUDIT_RETRY_DELAY_S", 0),
            mock.patch.object(
                mimo_audit,
                "ask_mimo",
                side_effect=[ConnectionResetError("reset by peer"), "VERDICT: PASS"],
            ) as ask_mimo,
            mock.patch.object(mimo_audit, "log"),
        ):
            verdict = mimo_audit.ask_mimo_with_retry("USER:\nfix code")

        self.assertEqual(verdict, "VERDICT: PASS")
        self.assertEqual(ask_mimo.call_count, 2)

    def test_claude_mimo_connection_reset_safe_fails_after_two_attempts(self) -> None:
        with (
            mock.patch.object(mimo_audit, "AUDIT_RETRY_DELAY_S", 0),
            mock.patch.object(mimo_audit, "ask_mimo", side_effect=ConnectionResetError("reset by peer")) as ask_mimo,
            mock.patch.object(mimo_audit, "log"),
        ):
            with self.assertRaisesRegex(RuntimeError, "after 2 attempts"):
                mimo_audit.ask_mimo_with_retry("USER:\nfix code")

        self.assertEqual(ask_mimo.call_count, 2)

    def test_claude_mimo_timeout_blocks_visibly_instead_of_silent_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "mimo-timeout",
                "turn_id": "turn-1",
                "last_assistant_message": "Completed implementation. " * 40,
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", side_effect=TimeoutError("read timeout")),
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn("GPT-5.6-Terra audit unavailable", data["reason"])
        self.assertEqual(verdict["mimo-timeout"]["verdict"], "ERROR")

    def test_claude_mimo_chat_turn_without_deliverable_claim_skips_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "claude-chat-turn",
                "last_assistant_message": "Here is status context with no edits or delivery claim. " * 20,
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry") as ask_mimo,
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        ask_mimo.assert_not_called()
        self.assertEqual(verdict["claude-chat-turn"]["verdict"], "SKIP_CHAT_TURN")

    def test_claude_mimo_block_chain_damper_allows_and_records_lesson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "iterations.json"
            state_path.write_text(json.dumps({"consecutive_blocks:claude-damper": 2}))
            payload = {
                "session_id": "claude-damper",
                "last_assistant_message": "I fixed and shipped the hook change. " * 20,
            }
            with (
                mock.patch.object(mimo_audit, "STATE_FILE", state_path),
                mock.patch.object(mimo_audit, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson") as record_lesson,
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: ISSUES\n1. Still noisy"),
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                state = json.loads(state_path.read_text())
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertNotIn("consecutive_blocks:claude-damper", state)
        self.assertEqual(verdict["claude-damper"]["verdict"], "DAMPER_ALLOW")
        record_lesson.assert_called()

    def test_call_mimo_uses_chatgpt_codex_cli(self) -> None:
        completed = mock.Mock(returncode=0, stdout="VERDICT: PASS\n", stderr="")
        with (
            mock.patch.object(api, "codex_binary", return_value="/app/codex"),
            mock.patch.object(api.subprocess, "run", return_value=completed) as run,
            mock.patch.object(api, "log") as audit_log,
        ):
            result = api.call_mimo("audit this", timeout=7, lane="checkpoint", system="verdict only")

        command = run.call_args.args[0]
        self.assertEqual(result, "VERDICT: PASS")
        self.assertEqual(command[0:2], ["/app/codex", "exec"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("read-only", command)
        self.assertIn("gpt-5.6-terra", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(run.call_args.kwargs["input"], "verdict only\n\naudit this")
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertIn("lane=checkpoint", audit_log.call_args.args[1])

    def test_api_response_extractors(self) -> None:
        self.assertEqual(
            api.anthropic_text({
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "VERDICT: PASS"},
                    {"type": "tool_use", "name": "noop"},
                ]
            }),
            "VERDICT: PASS",
        )

    def test_claude_autonomy_daemon_mines_local_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            path = repo / "hook.py"
            path.write_text("# FIXME: tighten\n")
            tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertEqual(tasks[0]["title"], "Resolve backlog marker in hook.py")
        self.assertEqual(tasks[0]["cwd"], tmp)

    def test_claude_autonomy_daemon_mines_local_tasks_without_rg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            path = repo / "hook.py"
            path.write_text("# FIXME: tighten\n")
            with mock.patch.object(autonomy_daemon.shutil, "which", return_value=None):
                tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertTrue(tasks)
        self.assertEqual(tasks[0]["title"], "Resolve backlog marker in hook.py")
        self.assertEqual(tasks[0]["cwd"], tmp)

    def test_claude_autonomy_daemon_falls_back_when_rg_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            path = repo / "hook.py"
            path.write_text("# XXX: tighten\n")
            with (
                mock.patch.object(autonomy_daemon.shutil, "which", return_value="/usr/bin/rg"),
                mock.patch.object(autonomy_daemon, "safe_run", return_value=(0, "", "")),
            ):
                tasks = autonomy_daemon.mine_local_tasks(repo)

        self.assertTrue(tasks)
        self.assertEqual(tasks[0]["title"], "Resolve backlog marker in hook.py")
        self.assertEqual(tasks[0]["cwd"], tmp)

    def test_claude_autonomy_queue_holds_risky_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue.json"
            with (
                mock.patch.object(autonomy, "QUEUE_PATH", queue),
                mock.patch.object(autonomy, "STOP_PATH", Path(tmp) / "STOP"),
                mock.patch.object(autonomy, "SECRETS_ENV", Path(tmp) / "missing_env"),
            ):
                added = autonomy.enqueue_tasks(
                    [{
                        "title": "Deploy production fix",
                        "body": "Run ssh to update the VPS.",
                        "cwd": tmp,
                    }],
                    "test",
                )
                state = json.loads(queue.read_text())
                popped = autonomy.pop_next_task("claude", Path(tmp))

        self.assertEqual(added, 1)
        self.assertEqual(state["tasks"][0]["status"], "held")
        self.assertIn("risky autonomous task", state["tasks"][0]["hold_reason"])
        self.assertIsNone(popped)

    def test_claude_stop_orchestrator_accepts_same_turn_verdict_written_first(self) -> None:
        with (
            mock.patch.object(stop_orchestrator, "MIMO_WAIT_TIMEOUT_S", 2),
            mock.patch.object(
                stop_orchestrator,
                "load_json_locked",
                return_value={"orch-session": {"turn_id": "turn-1", "verdict": "PASS", "updated_at": time.time() - 10}},
            ),
            mock.patch.object(stop_orchestrator.time, "sleep"),
            mock.patch.object(stop_orchestrator, "log"),
        ):
            self.assertTrue(stop_orchestrator.wait_for_mimo_pass("orch-session", "turn-1"))

    def test_claude_stop_orchestrator_rejects_same_turn_chat_skip_without_waiting(self) -> None:
        with (
            mock.patch.object(stop_orchestrator, "MIMO_WAIT_TIMEOUT_S", 2),
            mock.patch.object(
                stop_orchestrator,
                "load_json_locked",
                return_value={
                    "orch-session": {
                        "turn_id": "turn-1",
                        "verdict": "SKIP_CHAT_TURN",
                        "updated_at": time.time() - 10,
                    }
                },
            ),
            mock.patch.object(stop_orchestrator.time, "sleep") as sleep,
            mock.patch.object(stop_orchestrator, "log"),
        ):
            self.assertFalse(stop_orchestrator.wait_for_mimo_pass("orch-session", "turn-1"))
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
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
from lib import audit_state  # noqa: E402
import autonomy_daemon  # noqa: E402
import session_start  # noqa: E402
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
    def test_mixed_user_result_stays_in_current_turn(self):
        path = self._transcript([
            {"type": "user", "message": {"content": "Old task"}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "now", "content": "FAILED current_test"},
                {"type": "text", "text": "Diagnose this failure"},
            ]}},
            {"type": "assistant", "message": {"content": "I am diagnosing it"}},
        ])
        for previous in (False, True):
            text = latest_task_slice_claude(path, include_previous=previous)
            self.assertIn("FAILED current_test", text)
            self.assertNotIn("PRIOR TURN CONTEXT", text)
            self.assertNotIn("Old task", text)

    def test_image_user_preserves_boundary_with_history(self):
        path = self._transcript([
            {"type": "user", "message": {"content": "Old task"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "OLD_RECEIPT"}]}},
            {"type": "user", "message": {"content": [{"type": "image", "source": {"type": "base64", "data": "fake"}}]}},
            {"type": "assistant", "message": {"content": "Inspecting the image"}},
        ])
        for previous in (False, True):
            text = latest_task_slice_claude(path, include_previous=previous)
            current = text.split("PRIOR TURN CONTEXT")[0]
            self.assertIn("[non-text user message]", current)
            self.assertNotIn("OLD_RECEIPT", current)
            self.assertNotIn("Old task", current)

    def test_native_followup_retains_labelled_historical_receipt(self):
        path = self._transcript([
            {"type": "user", "message": {"content": "Check hooks"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "check", "content": "PASS=4; parity=0"}]}},
            {"type": "assistant", "message": {"content": "The installed hooks passed"}},
            {"type": "user", "message": {"content": "And the suppression?"}},
            {"type": "assistant", "message": {"content": "The deployed fix passed that check"}},
        ])
        self.assertNotIn("PASS=4", latest_task_slice_claude(path))
        text = latest_task_slice_claude(path, include_previous=True)
        self.assertIn("PASS=4; parity=0", text)
        self.assertIn("PRIOR TURN CONTEXT", text)

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

    def test_prompt_noise_refinements_from_the_medium_night(self):
        # 2026-09-22: 342 findings at medium, 69 rejected — top rejection classes were peer/authorization disputes,
        # "no receipt" where a tool call existed, and re-raised answered findings.
        for needle in ("ATTRIBUTED relay is not that finding", "OWNER STANDING INSTRUCTIONS",
                       "confirm the slice\n  has NO tool call whose command would produce that evidence",
                       "CLOSES that\n  finding"):
            self.assertIn(needle, mimo_audit.AUDIT_PROMPT, needle)

    def test_owner_instructions_skip_questions_and_scheduled_prompts(self):
        from lib.claude_adapter import owner_instructions
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            rows = [
                {"type": "user", "timestamp": "2026-09-22T05:01:00Z", "message": {"content": "whats the issue here? you didnt fix it? deploy it"}},
                {"type": "user", "timestamp": "2026-09-22T04:49:00Z", "message": {"content": "One-hour check-in on the reviewer: Dom's words: do it"}},
                {"type": "user", "timestamp": "2026-09-22T03:00:00Z", "message": {"content": "Another Claude session sent a message:\n<cross-session-message>do whatever you need</cross-session-message>"}},
                {"type": "user", "timestamp": "2026-09-22T02:00:00Z", "message": {"content": "ok yes do it, use your judgement u have full approval"}},
                {"type": "user", "timestamp": "2026-09-22T01:00:00Z", "message": {"content": "how to solve this? " + "x" * 500 + " ship it"}},
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            got = owner_instructions(str(path), limit=5)
            self.assertEqual([t for _, t in got], ["ok yes do it, use your judgement u have full approval"])

    def test_prompt_explains_elided_tool_output_marker(self):
        # The compactor's receipt excerpts are prefixed with this marker; the
        # reviewer must not read the elision itself as missing evidence.
        self.assertIn("[kept from elided\n  output]", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Never flag a claim as unreceipted only because", mimo_audit.AUDIT_PROMPT)
        self.assertIn("phantom truncation", mimo_audit.AUDIT_PROMPT)

    def test_mimo_prompt_blocks_bloat_and_random_debugging(self) -> None:
        self.assertIn("file/module sprawl", mimo_audit.AUDIT_PROMPT)
        self.assertIn("speculative abstractions", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Hardcoded or silo-specific fixes", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Semble for code discovery", mimo_audit.AUDIT_PROMPT)
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

    def test_claude_mimo_prompt_flags_overclaimed_plan_items(self) -> None:
        # 2026-09-26: "special move done" passed review when 2 of 10 moves existed, and
        # "being built as of now" passed when the only support was the lane's own order.
        self.assertIn("Plan-item and progress claims", mimo_audit.AUDIT_PROMPT)
        self.assertIn('"2 of 10 done"', mimo_audit.AUDIT_PROMPT)
        self.assertIn("The agent's own list of parts is not the definition", mimo_audit.AUDIT_PROMPT)
        self.assertIn("overrides the follow-up\n  and earlier-turn evidence exceptions", mimo_audit.AUDIT_PROMPT)
        self.assertIn("own outgoing order", mimo_audit.AUDIT_PROMPT)
        self.assertIn('"Ordered", "asked X to", "target Sunday" are honest', mimo_audit.AUDIT_PROMPT)

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

    def test_review_calibration_preserves_real_failure_gates(self) -> None:
        for phrase in ("exact URL/resource", "negative test", "proposed wording",
                       "still flag unsafe recommendations", "internal pipeline success alone"):
            self.assertIn(phrase, mimo_audit.AUDIT_PROMPT)

    def test_followup_receipt_exception_keeps_new_claims_gated(self) -> None:
        for phrase in ("missing repeated receipts alone is NOT a blocker", "does not establish truth",
                       "Still block contradictions", "fresh check without output", "For NEW claims"):
            self.assertIn(phrase, mimo_audit.AUDIT_PROMPT)

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
        self.assertIn("find code with Semble", context)
        self.assertIn("concise coverage receipt", context)
        self.assertIn("two-pass review", context)

    def test_claude_session_baseline_includes_context_efficiency(self) -> None:
        self.assertIn("capped output", session_start.SESSION_BASELINE)
        self.assertIn("Avoid duplicate parallel exploration", session_start.SESSION_BASELINE)
        self.assertIn("compact context packet", session_start.SESSION_BASELINE)
        self.assertIn("find code with Semble", session_start.SESSION_BASELINE)
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
                mock.patch.object(audit_state, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", tmp_path  / "ledger.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", tmp_path / "verdict.json"),
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
                mock.patch.object(audit_state, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", tmp_path  / "ledger.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", side_effect=TimeoutError("read timeout")),
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn(f"{api.AUDITOR_NAME} audit unavailable", data["reason"])
        self.assertEqual(verdict["mimo-timeout"]["verdict"], "ERROR")

    def test_claude_mimo_chat_turn_without_deliverable_claim_skips_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "session_id": "claude-chat-turn",
                "last_assistant_message": "Here is status context with no edits or delivery claim. " * 20,
            }
            with (
                mock.patch.object(audit_state, "STATE_FILE", tmp_path / "iterations.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", tmp_path  / "ledger.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry") as ask_mimo,
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        ask_mimo.assert_not_called()
        self.assertEqual(verdict["claude-chat-turn"]["verdict"], "SKIP_CHAT_TURN")

    def test_claude_mimo_block_chain_damper_allows_without_recording_lesson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "iterations.json"
            state_path.write_text(json.dumps({"consecutive_blocks:claude-damper": 2}))
            payload = {
                "session_id": "claude-damper",
                "last_assistant_message": "I fixed and shipped the hook change. " * 20,
            }
            with (
                mock.patch.object(audit_state, "STATE_FILE", state_path),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", state_path.parent / "ledger.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", tmp_path / "verdict.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson") as record_lesson,
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: ISSUES\n1. Still noisy"),
            ):
                rc, out = run_main_with_stdin(mimo_audit.main, payload)
                state = json.loads(state_path.read_text())
                verdict = json.loads((tmp_path / "verdict.json").read_text())

        self.assertEqual(rc, 0)
        # Updated for audit F13: the damped release is allow + a visible systemMessage (never a block decision).
        self.assertNotIn("decision", json.loads(out))
        self.assertIn("unresolved", json.loads(out)["systemMessage"])
        self.assertNotIn("consecutive_blocks:claude-damper", state)
        self.assertEqual(verdict["claude-damper"]["verdict"], "DAMPER_ALLOW")
        # Round 3 of a chain is re-litigation, not review: it must not feed Active Lessons.
        record_lesson.assert_not_called()

    def test_call_mimo_uses_chatgpt_codex_cli(self) -> None:
        completed = (0, "VERDICT: PASS\n", "")
        with (
            mock.patch.object(api, "AUDIT_TRANSPORT", "codex"),
            mock.patch.object(api, "codex_binary", return_value="/app/codex"),
            mock.patch.object(api, "safe_run", return_value=completed) as run,
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
        self.assertEqual(run.call_args.kwargs["input_text"], "verdict only\n\naudit this")
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

    def test_hook_env_prefers_claude_file_over_codex_fallback(self) -> None:
        # Regression: the Claude install read only ~/.codex/secrets/hook_env, so
        # CODEX_HOOK_AUTO_PR=0 in ~/.claude/secrets/hook_env was ignored.
        with tempfile.TemporaryDirectory() as tmp:
            claude_env = Path(tmp) / "claude_env"
            codex_env = Path(tmp) / "codex_env"
            claude_env.write_text("# comment\nCODEX_HOOK_AUTO_PR=0\n")
            codex_env.write_text("CODEX_HOOK_AUTO_PR=1\nSHARED_ONLY='x'\n")
            with (
                mock.patch.object(autonomy, "SECRETS_ENV", claude_env),
                mock.patch.object(autonomy, "SECRETS_ENV_FALLBACK", codex_env),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("CODEX_HOOK_AUTO_PR", None)
                self.assertEqual(autonomy.hook_env("CODEX_HOOK_AUTO_PR", "1"), "0")
                self.assertEqual(autonomy.hook_env("SHARED_ONLY"), "x")
                self.assertEqual(autonomy.hook_env("MISSING", "d"), "d")
                os.environ["CODEX_HOOK_AUTO_PR"] = "1"
                self.assertEqual(autonomy.hook_env("CODEX_HOOK_AUTO_PR", "0"), "1")

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

        self.assertEqual(added, 1)
        self.assertEqual(state["tasks"][0]["status"], "held")
        self.assertIn("risky autonomous task", state["tasks"][0]["hold_reason"])

if __name__ == "__main__":
    unittest.main()


class ClaudeAuditTransportTests(unittest.TestCase):
    """Claude-side audit runs on the Claude CLI by default and can never recurse."""

    def test_call_mimo_routes_to_claude_cli_when_transport_is_claude(self) -> None:
        import lib.api as api

        with mock.patch.object(api, "AUDIT_TRANSPORT", "claude"), \
             mock.patch.object(api, "call_claude_cli", return_value="VERDICT: PASS") as cli, \
             mock.patch.object(api, "codex_binary", side_effect=AssertionError("codex must not be called")):
            self.assertEqual(api.call_mimo("audit this", lane="stop"), "VERDICT: PASS")
        cli.assert_called_once()
        self.assertEqual(cli.call_args.kwargs["lane"], "stop")

    def test_call_mimo_keeps_codex_path_when_transport_is_codex(self) -> None:
        import lib.api as api

        with mock.patch.object(api, "AUDIT_TRANSPORT", "codex"), \
             mock.patch.object(api, "call_claude_cli", side_effect=AssertionError("claude must not be called")), \
             mock.patch.object(api, "codex_binary", side_effect=RuntimeError("stop here")):
            with self.assertRaisesRegex(RuntimeError, "stop here"):
                api.call_mimo("audit this")

    def test_claude_cli_child_sets_guard_env_and_disallows_mutating_tools(self) -> None:
        import lib.api as api

        captured: dict = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return 0, "VERDICT: PASS\n", ""

        with mock.patch.object(api, "claude_binary", return_value="/fake/claude"), \
             mock.patch.object(api, "safe_run", side_effect=fake_run):
            self.assertEqual(api.call_claude_cli("audit this"), "VERDICT: PASS")
        self.assertEqual(captured["env"].get(api.AUDIT_CHILD_ENV), "1")
        self.assertIn("--safe-mode", captured["command"])
        self.assertIn("--no-session-persistence", captured["command"])
        self.assertIn("--effort", captured["command"])
        self.assertIn("--disallowedTools", captured["command"])
        self.assertIn("Bash Edit Write NotebookEdit", captured["command"])

    def test_claude_cli_injects_setup_token_from_hook_env(self) -> None:
        import lib.api as api

        captured: dict = {}

        def fake_run(command, **kwargs):
            captured["env"] = kwargs["env"]
            return 0, "VERDICT: PASS\n", ""

        with mock.patch.object(api, "claude_binary", return_value="/fake/claude"), \
             mock.patch.object(api, "hook_env", side_effect=lambda k, d="": "tok-123" if k == "CLAUDE_CODE_OAUTH_TOKEN" else d), \
             mock.patch.object(api, "safe_run", side_effect=fake_run):
            api.call_claude_cli("audit this")
        self.assertEqual(captured["env"].get("CLAUDE_CODE_OAUTH_TOKEN"), "tok-123")

    def test_hooks_exit_immediately_inside_an_audit_child(self) -> None:
        import lib.api as api

        with mock.patch.dict(api.os.environ, {api.AUDIT_CHILD_ENV: "1"}):
            with self.assertRaises(SystemExit) as ctx:
                api.exit_if_audit_child()
        self.assertEqual(ctx.exception.code, 0)
        with mock.patch.dict(api.os.environ, {}, clear=False):
            api.os.environ.pop(api.AUDIT_CHILD_ENV, None)
            api.exit_if_audit_child()  # no exit outside a child

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import mimo_audit  # noqa: E402
from lib.transcript import compact_latest_task, compact_task_entries, latest_task_slice  # noqa: E402


class ReviewEvidenceTests(unittest.TestCase):
    def test_middle_failure_survives_later_unrelated_green_output(self):
        entries = [('USER', 'Fix the swipe workflow'), ('TOOL OUTPUT', 'initial check passed')]
        entries += [('TOOL OUTPUT', '[browser] swipe test\nAssertionError: swipe heavy consumes stamina')]
        entries += [('TOOL OUTPUT', f'poll-{i} ' + 'progress ' * 300) for i in range(8)]
        entries += [('TOOL OUTPUT', 'unrelated unit checks: 14 passed'), ('ASSISTANT', 'All fixed.')]
        text = compact_task_entries(entries, 4000)
        self.assertIn('CHECK HISTORY', text)
        self.assertIn('AssertionError: swipe heavy consumes stamina', text)
        self.assertIn('unrelated unit checks: 14 passed', text)
        self.assertLessEqual(len(text), 4000)
        entries += [('TOOL OUTPUT', 'same swipe browser check: PASS exit_code=0')]
        self.assertIn('same swipe browser check: PASS', compact_task_entries(entries, 4000))

    def test_failure_inside_long_tool_output_survives_first_compaction(self):
        text = self.slice([
            {'type': 'message', 'role': 'user', 'content': 'Fix the failing workflow'},
            {'type': 'function_call_output', 'output': 'before ' * 300 +
             '\nAssertionError: WORKFLOW_FAILED\n' + 'after ' * 300},
            {'type': 'message', 'role': 'assistant', 'content': 'The issue was reported.'},
        ])
        self.assertIn('AssertionError: WORKFLOW_FAILED', text)

    def test_short_completion_is_audited_but_chat_still_skips(self):
        for message, expected in (("I deployed it.", "ISSUES"), ("Thanks.", "SKIP_TOO_SHORT")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = {"session_id": "short", "last_assistant_message": message}
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()), \
                     mock.patch.object(mimo_audit, "STATE_FILE", root / "iterations.json"), \
                     mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", root / "ledger.json"), \
                     mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"), \
                     mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"), \
                     mock.patch.object(mimo_audit, "record_lesson"), \
                     mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: ISSUES\n1. No deployment evidence") as audit:
                    try:
                        mimo_audit.main()
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 0)
                verdict = json.loads((root / "verdict.json").read_text())["short"]["verdict"]
                self.assertEqual(verdict, expected)
                self.assertEqual(audit.call_count, int(expected == "ISSUES"))

    def test_followup_can_reuse_bounded_previous_receipts(self):
        entries = [("USER", "Check hooks"), ("TOOL OUTPUT", "PASS=4 skips=5; parity=0; sk-SECRET"),
                   ("ASSISTANT", "Earlier check passed"), ("USER", "Is suppression working?"),
                   ("ASSISTANT", "The earlier check confirms the deployed fix")]
        self.assertNotIn("PASS=4", compact_latest_task(entries, 4000))
        text = compact_latest_task(entries, 4000, include_previous=True)
        self.assertIn("PASS=4 skips=5", text)
        self.assertIn("historical data, not a new request or fresh verification", text)
        self.assertNotIn("SECRET", text)
        self.assertTrue(text.startswith("USER:\nIs suppression working?"))
        self.assertLessEqual(len(text), 4000)

    def test_previous_receipts_do_not_replace_current_failures(self):
        entries = [("USER", "Old check"), ("TOOL OUTPUT", "OLD_PASS"),
                   ("USER", "Fresh check"), ("TOOL OUTPUT", "FAILED current_test"),
                   ("ASSISTANT", "All passed")]
        text = compact_latest_task(entries, 4000, include_previous=True)
        self.assertNotIn("OLD_PASS", text)
        self.assertIn("FAILED current_test", text)

    def test_history_is_one_turn_only_and_respects_small_budgets(self):
        entries = [("USER", "Older"), ("TOOL OUTPUT", "STALE_RECEIPT"),
                   ("USER", "Previous"), ("ASSISTANT", "No checks"),
                   ("USER", "Now"), ("ASSISTANT", "Follow-up")]
        self.assertNotIn("STALE_RECEIPT", compact_latest_task(entries, 4000, True))
        entries[3] = ("TOOL OUTPUT", "evidence " * 1000)
        for limit in (100, 599, 600, 4000):
            self.assertLessEqual(len(compact_latest_task(entries, limit, True)), limit)

    def slice(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text("\n".join(json.dumps({"type": "response_item", "payload": row}) for row in records))
            return latest_task_slice(str(path))

    def test_custom_tool_receipt_keeps_command_exit_and_output(self):
        text = self.slice([
            {"type": "message", "role": "user", "content": "Check tests"},
            {"type": "custom_tool_call", "call_id": "call-1", "name": "exec", "input": "pytest -q"},
            {"type": "custom_tool_call_output", "call_id": "call-1", "output": [
                {"type": "input_text", "text": '{"exit_code": 1, "output": "FAILED test_real_bug"}'},
            ]},
            {"type": "message", "role": "assistant", "content": "All tests passed"},
        ])
        for receipt in ("pytest -q", "call-1", '"exit_code": 1', "FAILED test_real_bug", "All tests passed"):
            self.assertIn(receipt, text)

    def test_hook_feedback_does_not_replace_current_request(self):
        text = self.slice([
            {"type": "message", "role": "user", "content": "Check the model; no edits requested"},
            {"type": "message", "role": "assistant", "content": "Checking"},
            {"type": "message", "role": "user", "content": "<hook_prompt>Show model evidence</hook_prompt>"},
            {"type": "function_call", "call_id": "fix", "name": "exec", "arguments": "print model"},
            {"type": "function_call_output", "call_id": "fix", "output": "gpt-5.6-terra high"},
        ])
        self.assertIn("USER:\nCheck the model", text)
        self.assertIn("PRIOR REVIEW (not a new request)", text)
        self.assertIn("Show model evidence", text)
        self.assertIn("gpt-5.6-terra high", text)

    def test_new_user_request_drops_old_work(self):
        text = self.slice([
            {"type": "message", "role": "user", "content": "OLD_TASK"},
            {"type": "function_call_output", "output": "OLD_RECEIPT"},
            {"type": "message", "role": "user", "content": "New question"},
            {"type": "message", "role": "assistant", "content": "New answer"},
        ])
        self.assertNotIn("OLD_", text)
        self.assertIn("New question", text)

    def test_compactor_preserves_distinct_receipts_and_cap(self):
        entries = [("USER", "Quoted context " + "U" * 5000 + "\nActual request: verify status")]
        entries += [("TOOL OUTPUT", f"command-{i}\n" + "x" * 5000 + f"\nresult-{i}") for i in range(3)]
        entries += [("ASSISTANT", "Current answer " + "A" * 5000)]
        text = compact_task_entries(entries, 4000)
        self.assertLessEqual(len(text), 4000)
        self.assertIn("Actual request: verify status", text)
        for i in range(3):
            self.assertIn(f"command-{i}", text)
            self.assertIn(f"result-{i}", text)
        self.assertEqual(text.count("Current answer"), 1)

    def test_real_prompt_receives_receipt_and_calibration(self):
        text = self.slice([
            {"type": "message", "role": "user", "content": "Are tests green?"},
            {"type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "12 passed; exit_code=0"}]},
            {"type": "message", "role": "assistant", "content": "Yes, 12 passed"},
        ])
        with mock.patch.object(mimo_audit, "call_mimo", return_value="VERDICT: PASS") as call:
            self.assertEqual(mimo_audit.ask_mimo(text), "VERDICT: PASS")
        prompt = call.call_args.args[0]
        for phrase in ("12 passed; exit_code=0", "not new user tasks", "Do not repeat a resolved finding",
                       "Missing context alone", "Still block visible broken code",
                       "An acknowledged past procedural mistake cannot be undone",
                       "Acknowledgement alone does not resolve current defects",
                       "repeated violations in new actions", "unsupported completion claims"):
            self.assertIn(phrase, prompt)

    def test_early_verification_survives_later_poll_outputs(self):
        entries = [("USER", "Verify both installed hooks")]
        entries += [("TOOL OUTPUT", "quality command: unittest discover\nRan 99 tests: OK\nRan 55 tests: OK\nRuff: All checks passed\nparity mismatches=[]")]
        entries += [("TOOL OUTPUT", f"poll-{i}\n" + "progress " * 250 + f"\ncase-{i} matched=True") for i in range(6)]
        entries += [("ASSISTANT", "Both suites passed; the calibration cases matched.")]
        text = compact_task_entries(entries, 4000)
        for receipt in ("Ran 99 tests: OK", "Ran 55 tests: OK", "All checks passed", "parity mismatches=[]", "case-5 matched=True"):
            self.assertIn(receipt, text)
        self.assertLessEqual(len(text), 4000)
        self.assertIn("omitted tool outputs", text)

    def test_receipts_remain_data_and_latest_failure_is_retained(self):
        entries = [("USER", "Check tests")]
        entries += [("TOOL OUTPUT", "old run: PASS"), ("ASSISTANT", "progress " * 2000)]
        entries += [("TOOL OUTPUT", 'latest run: exit_code=1\nFAILED test_real_bug\nsk-SECRET123')]
        entries += [("ASSISTANT", "All tests passed")]
        text = compact_task_entries(entries, 4000)
        self.assertIn("TOOL OUTPUT", text)
        self.assertIn("FAILED test_real_bug", text)
        self.assertNotIn("SECRET", text)
        self.assertIn("All tests passed", text)

    def test_receipt_redaction(self):
        text = self.slice([{"type": "function_call_output", "output": "sk-SECRET123 ghp_SECRET456 tp-SECRET789"}])
        self.assertNotIn("SECRET", text)

    def test_terminal_wrappers_do_not_bury_suite_results(self):
        records = [
            {"type": "message", "role": "user", "content": "Verify hooks"},
            {"type": "custom_tool_call", "call_id": "checks", "name": "exec", "input": "run gates " + "x" * 2000},
            {"type": "custom_tool_call_output", "call_id": "checks", "output": [
                {"type": "input_text", "text": json.dumps({"chunk_id": "a", "exit_code": 0, "output":
                    "unittest discover\n" + "-" * 70 + "\nRan 99 tests: OK\n" + "-" * 70 +
                    "\nRan 55 tests: OK\nRuff: All checks passed\nparity mismatches=[]"})},
            ]},
        ]
        records += [{"type": "function_call_output", "call_id": f"poll-{i}",
                     "output": "progress " * 250 + f"\ncase-{i} matched=True"} for i in range(6)]
        records += [{"type": "message", "role": "assistant", "content": "Both suites passed"}]
        text = self.slice(records)
        for phrase in ("Ran 99 tests: OK", "Ran 55 tests: OK", "All checks passed", "mismatches=[]", '"exit_code": 0'):
            self.assertIn(phrase, text)
        self.assertLessEqual(len(text), 4000)

    def test_structured_failed_output_preserves_failure_and_ordinary_json(self):
        text = self.slice([
            {"type": "function_call_output", "output": json.dumps(
                {"chunk_id": "bad", "exit_code": 1, "output": "FAILED test_real_bug"})},
            {"type": "function_call_output", "output": '{"output": "domain data", "status": "rejected"}'},
            {"type": "function_call_output", "output": '{"chunk_id": "domain-id", "output": "report", "status": "rejected"}'},
            {"type": "function_call_output", "output": "before\n---\nafter"},
        ])
        for phrase in ('"exit_code": 1', "FAILED test_real_bug", '"status": "rejected"'):
            self.assertIn(phrase, text)
        self.assertEqual(text.count('"status": "rejected"'), 2)
        self.assertIn("before\n---\nafter", text)

    def test_crowded_budget_preserves_latest_failure(self):
        entries = [("USER", "Request " + "u" * 2000)]
        entries += [("TOOL OUTPUT", "command " + "x" * 3000) for _ in range(3)]
        entries += [("TOOL OUTPUT", "command " + "x" * 400 +
                     '\nexit_code: 1\nFAILED test_real_bug\n' + "y" * 3000)]
        entries += [("ASSISTANT", "All passed " + "a" * 3000)]
        text = compact_task_entries(entries, 4000)
        for phrase in ("exit_code: 1", "FAILED test_real_bug", "All passed"):
            self.assertIn(phrase, text)
        self.assertLessEqual(len(text), 4000)


if __name__ == "__main__":
    unittest.main()

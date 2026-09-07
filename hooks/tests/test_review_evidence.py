from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import mimo_audit  # noqa: E402
from lib.transcript import compact_task_entries, latest_task_slice  # noqa: E402


class ReviewEvidenceTests(unittest.TestCase):
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
                       "Missing context alone", "Still block visible broken code"):
            self.assertIn(phrase, prompt)

    def test_receipt_redaction(self):
        text = self.slice([{"type": "function_call_output", "output": "sk-SECRET123 ghp_SECRET456 tp-SECRET789"}])
        self.assertNotIn("SECRET", text)


if __name__ == "__main__":
    unittest.main()

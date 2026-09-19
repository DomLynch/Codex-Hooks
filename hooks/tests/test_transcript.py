from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib.transcript import _bounded_compact, deterministic_findings, latest_task_slice  # noqa: E402


def message(role: str, text: str) -> str:
    return json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    })


def tool_output(text: str) -> str:
    return json.dumps({
        "type": "response_item",
        "payload": {"type": "function_call_output", "output": text},
    })


class TranscriptSliceTests(unittest.TestCase):
    def test_file_link_without_content_claim_is_not_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text("{}")
            findings = deterministic_findings([], f"See [result]({path})", tmp)
        self.assertEqual(findings, [])

    def test_failed_check_still_requires_disposition(self) -> None:
        entries = [("TOOL OUTPUT", '[run] pytest -q\n{"exit_code": 1}\nFAILED test_case')]
        findings = deterministic_findings(entries, "All done.", ".")
        self.assertEqual(len(findings), 1)
        self.assertIn("A check failed", findings[0])

    def test_bounded_compact_handles_tiny_budget(self) -> None:
        self.assertLessEqual(len(_bounded_compact("x" * 100, 10)), 10)

    def test_huge_first_user_entry_is_hard_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("\n".join([
                message("user", "A" * 200_000),
                message("assistant", "done"),
            ]))

            text = latest_task_slice(str(transcript))

        self.assertLessEqual(len(text), 4000)
        self.assertIn("[head truncated]", text)
        self.assertIn("older task details omitted", text)

    def test_hook_feedback_message_becomes_current_user_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("\n".join([
                message("user", "ready to merge this branch"),
                message("assistant", "old task output"),
                message(
                    "user",
                    '<hook_prompt hook_run_id="stop:4:/Users/domininclynch/.codex/config.toml">'
                    "[AAA Milestone Gate]\n\nnpm test failed"
                    "</hook_prompt>\n"
                    "another dev is also working on this",
                ),
            ]))

            text = latest_task_slice(str(transcript))

        self.assertIn("USER:", text)
        self.assertIn("another dev is also working on this", text)
        self.assertNotIn("ready to merge this branch", text)
        self.assertNotIn("npm test failed", text)

    def test_hook_only_feedback_preserves_request_and_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("\n".join([
                message("user", "merge this now"),
                message("assistant", "old task output"),
                message(
                    "user",
                    '<hook_prompt hook_run_id="stop:4:/Users/domininclynch/.codex/config.toml">'
                    "[AAA Milestone Gate]\n\nnpm test failed"
                    "</hook_prompt>",
                ),
            ]))

            text = latest_task_slice(str(transcript))

        self.assertIn("PRIOR REVIEW (not a new request)", text)
        self.assertIn("merge this now", text)
        self.assertIn("npm test failed", text)

    def test_compacted_tool_call_marks_slice_elision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("\n".join([
                message("user", "edit app.py"),
                json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "apply_patch",
                        "arguments": "new_string=" + ("A" * 10_000),
                    },
                }),
                message("assistant", "quality files=1 ok=True"),
            ]))

            text = latest_task_slice(str(transcript))

        self.assertIn("slice-elided by compactor", text)
        self.assertNotIn("...[omitted]...", text)
        self.assertIn("quality files=1 ok=True", text)

    def test_compaction_preserves_middle_verification_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("\n".join([
                message("user", "verify and deploy " + "U" * 5000),
                tool_output("Ran 70 tests in 1.4s\nOK\nAll checks passed!"),
                message("assistant", "Final verified summary. " + "A" * 5000),
            ]))

            text = latest_task_slice(str(transcript))

        self.assertLessEqual(len(text), 4000)
        self.assertIn("verification receipts preserved", text)
        self.assertIn("Ran 70 tests", text)
        self.assertIn("All checks passed", text)
        self.assertIn("Final verified summary", text)


if __name__ == "__main__":
    unittest.main()

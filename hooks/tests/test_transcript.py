from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib.transcript import latest_task_slice  # noqa: E402


def message(role: str, text: str) -> str:
    return json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    })


class TranscriptSliceTests(unittest.TestCase):
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

    def test_hook_only_feedback_still_resets_current_user_boundary(self) -> None:
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

        self.assertIn("[hook feedback omitted]", text)
        self.assertNotIn("merge this now", text)
        self.assertNotIn("npm test failed", text)

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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib.transcript import _bounded_compact, deterministic_findings, latest_task_slice  # noqa: E402
from lib import transcript as parser  # noqa: E402


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
    def test_review_continuations_keep_the_original_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'turn.jsonl'
            records = []
            for turn, text in [('task', 'Fix all 18 tasks.'), ('retry1', '<hook_prompt>Still incomplete</hook_prompt>'),
                               ('retry2', '<hook_prompt>Try again</hook_prompt>')]:
                records.extend([json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}}),
                                message('user', text)])
            path.write_text('\n'.join(records))
            self.assertIn('Fix all 18 tasks.', latest_task_slice(str(path), preserve_request=True))

    def test_snapshot_is_reused_and_refreshes_after_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'turn.jsonl'
            path.write_text(message('user', 'Original request.'))
            with mock.patch.object(parser.mmap, 'mmap', wraps=parser.mmap.mmap) as read:
                latest_task_slice(str(path))
                latest_task_slice(str(path), preserve_request=True)
                self.assertEqual(read.call_count, 1)
                with path.open('a') as file:
                    file.write('\n' + message('user', 'Cancel the original request.'))
                self.assertIn('Cancel the original request.', latest_task_slice(str(path)))
                self.assertEqual(read.call_count, 2)

    def test_old_turns_are_not_normalized_and_prior_context_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'turn.jsonl'
            def boundary(turn):
                return json.dumps({'type': 'turn_context', 'payload': {'turn_id': turn}})
            path.write_text('\n'.join([
                boundary('old'), message('user', 'Obsolete request'), tool_output('OBSOLETE'),
                boundary('previous'), message('user', 'Verify deployment.'),
                tool_output('Release abc123 is live.'), boundary('previous'),
                message('user', 'Check its revision too.'),
                boundary('current'), message('user', 'Summarize that verification.'),
                boundary('current'), message('user', 'Keep it concise.'),
                message('assistant', 'Release abc123 was verified.'),
            ]))
            with mock.patch.object(parser, 'tool_entry', wraps=parser.tool_entry) as normalize:
                text = latest_task_slice(str(path), preserve_request=True, include_previous=True)
            self.assertFalse(any(c.args[0].get('output') == 'OBSOLETE' for c in normalize.call_args_list))
            for expected in ('Verify deployment.', 'Release abc123 is live.',
                             'Summarize that verification.', 'Keep it concise.'):
                self.assertIn(expected, text)

    def test_review_keeps_same_turn_requests_and_earlier_receipts(self) -> None:
        for event_type in ('event_msg', 'turn_context'):
            with self.subTest(event_type=event_type), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'turn.jsonl'
                boundary = json.dumps({'type': event_type, 'payload': {'type': 'task_started', 'turn_id': 'active'}})
                request = 'Create all 18 separate tasks. ' + 'Details. ' * 500
                path.write_text('\n'.join([
                    message('user', 'Obsolete task'), boundary, message('user', request),
                    tool_output('Verified task count: 0'), boundary,
                    message('user', 'Keep it lean.'),
                    message('user', '<hook_prompt>Prior review</hook_prompt>\nCodex only.'),
                    message('assistant', 'I saved the handover; no tasks were created.'),
                ]))
                text = latest_task_slice(str(path), preserve_request=True)
                for expected in (request, 'Keep it lean.', 'Codex only.', 'Verified task count: 0'):
                    self.assertIn(expected, text)
                self.assertNotIn('Obsolete task', text)
                self.assertLessEqual(len(text), len(request) + 4200)

    def test_review_new_turn_does_not_revive_previous_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'turn.jsonl'
            records = []
            for turn, request in [('old', 'Deploy the old release.'), ('new', 'Audit only; no edits.')]:
                records.extend([json.dumps({'type': 'turn_context', 'payload': {'turn_id': turn}}),
                                message('user', request)])
            records.extend([message('user', 'Be concise.'), message('assistant', 'Two findings remain.')])
            path.write_text('\n'.join(records))
            text = latest_task_slice(str(path), preserve_request=True)
            self.assertIn('Audit only; no edits.', text)
            self.assertIn('Be concise.', text)
            self.assertNotIn('Deploy the old release.', text)
            # Non-review callers retain the latest-message boundary, including pause handling.
            self.assertNotIn('Audit only; no edits.', latest_task_slice(str(path)))

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

    def test_environment_context_does_not_replace_the_user_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("\n".join([
                message("user", "ok standby for now"),
                message("user", "<environment_context>\n<current_date>2026-09-23</current_date>\n</environment_context>"),
                message("assistant", "Standby, no work started."),
            ]))
            text = latest_task_slice(str(transcript))
        self.assertIn("ok standby for now", text)
        self.assertIn("Standby, no work started.", text)

    def test_environment_context_with_a_real_request_is_a_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("\n".join([
                message("user", "old request"),
                message("user", "<environment_context>\n</environment_context>\nFix the test"),
            ]))
            text = latest_task_slice(str(transcript))
        self.assertIn("Fix the test", text)
        self.assertNotIn("old request", text)

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

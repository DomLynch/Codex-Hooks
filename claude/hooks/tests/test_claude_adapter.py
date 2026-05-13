from __future__ import annotations

import json
import sys
import tempfile
import unittest
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
from lib import autonomy  # noqa: E402
from lib import api  # noqa: E402
from lib.transcript import latest_task_slice as latest_codex_style_task_slice  # noqa: E402


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
        self.assertIn("Random debugging", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Audit-only or review-only requests", mimo_audit.AUDIT_PROMPT)

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


if __name__ == "__main__":
    unittest.main()

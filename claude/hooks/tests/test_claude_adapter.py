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
import trident_notify  # noqa: E402
from lib import autonomy  # noqa: E402
from lib import api  # noqa: E402


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

    def test_mimo_bare_verdicts_are_normalized(self) -> None:
        self.assertEqual(mimo_audit.normalize_verdict("PASS"), "VERDICT: PASS")
        self.assertEqual(
            mimo_audit.normalize_verdict("issues\n1. Missing regression test"),
            "VERDICT: ISSUES\n1. Missing regression test",
        )

    def test_mimo_audit_prefers_openclaw_transport(self) -> None:
        with (
            mock.patch.object(mimo_audit, "call_mimo_via_openclaw", return_value="PASS") as openclaw,
            mock.patch.object(mimo_audit.urllib.request, "urlopen") as urlopen,
            mock.patch.object(mimo_audit, "log"),
        ):
            verdict = mimo_audit.ask_mimo("direct-api-key", "USER:\nfix code", max_tokens=20)

        self.assertEqual(verdict, "VERDICT: PASS")
        openclaw.assert_called_once()
        urlopen.assert_not_called()

    def test_trident_notify_sends_synthesized_turn_and_task_slice(self) -> None:
        path = self._transcript([
            {"type": "user", "message": {"content": "fix claude hook"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "working"},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "hook.py"}},
                    ],
                },
            },
        ])
        payload = trident_notify.normalize_payload({"session_id": "claude-session", "transcript_path": path, "cwd": "/tmp"})
        payload["_task_slice"] = trident_notify.current_task_slice(payload)
        message = trident_notify.compose_message(payload, {"verdict": "PASS", "preview": "VERDICT: PASS"})
        issues_message = trident_notify.compose_message(payload, {"verdict": "ISSUES", "preview": "VERDICT: ISSUES"})

        self.assertTrue(payload["turn_id"])
        self.assertIn(f"turn_id: {payload['turn_id']}", message)
        self.assertIn("--- current task slice ---", message)
        self.assertIn("fix claude hook", message)
        self.assertIn("TOOL CALL: Edit", message)
        self.assertIn("may log it, queue a small", message)
        self.assertIn("Do NOT queue new work", issues_message)
        self.assertNotIn("turn_id: ?", message)

    def test_call_mimo_prefers_openclaw_transport(self) -> None:
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = json.dumps({"ok": True, "outputs": [{"text": "VERDICT: PASS"}]})
        with (
            mock.patch.object(api.shutil, "which", return_value="/usr/bin/openclaw"),
            mock.patch.object(api.subprocess, "run", return_value=proc) as run,
            mock.patch.object(api, "load_api_key") as load_key,
        ):
            result = api.call_mimo("audit this", timeout=7)

        self.assertEqual(result, "VERDICT: PASS")
        load_key.assert_not_called()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], ["/usr/bin/openclaw", "infer", "model", "run"])
        self.assertNotIn("--gateway", cmd)
        self.assertIn("mimio/mimo-v2.5-pro", cmd)

    def test_claude_autonomy_daemon_parses_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = autonomy_daemon.parse_json_tasks(
                '[{"title":"Tighten hook test","body":"Run unittest","source_file":"hooks/x.py"}]',
                Path(tmp),
            )

        self.assertEqual(tasks[0]["title"], "Tighten hook test")
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

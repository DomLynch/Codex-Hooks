from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import mimo_audit  # noqa: E402
from lib.transcript import (  # noqa: E402
    deterministic_findings, doc_only_turn, edited_paths, latest_task_entries,
    sensitive_paths, turn_diff,
)


class CodexReviewIntegrationTests(unittest.TestCase):
    def test_native_codex_edit_path_feeds_scoped_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(["git", "-C", tmp, "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", tmp, "config", "user.name", "Hook Test"], check=True)
            target = root / "renderer.py"
            target.write_text("value = 1\n")
            subprocess.run(["git", "-C", tmp, "add", "renderer.py"], check=True)
            subprocess.run(["git", "-C", tmp, "commit", "-qm", "baseline"], check=True)
            target.write_text("value = 2\n")
            transcript = root / "turn.jsonl"
            records = [
                {"type": "message", "role": "user", "content": "Fix renderer"},
                {"type": "custom_tool_call", "call_id": "edit-1", "name": "functions.exec",
                 "input": f"await tools.apply_patch('*** Update File: {target}\\n')"},
                {"type": "custom_tool_call_output", "call_id": "edit-1", "output": "patch applied"},
                {"type": "message", "role": "assistant", "content": "Changed renderer"},
            ]
            transcript.write_text("\n".join(json.dumps({"type": "response_item", "payload": row}) for row in records))
            entries = latest_task_entries(str(transcript))
            self.assertEqual(edited_paths(entries), [str(target)])
            diff = turn_diff(tmp, edited_paths(entries))
            self.assertIn("+value = 2", diff)
            self.assertNotIn("turn.jsonl", diff)
            fresh = root / "new_widget.py"
            fresh.write_text("feature_enabled = True\n")
            self.assertIn("feature_enabled = True", turn_diff(tmp, [str(fresh)]))
            records[1]["input"] = "await tools.apply_patch('" + "x" * 2000 + f"*** Update File: {target}\\n' )"
            transcript.write_text("\n".join(json.dumps({"type": "response_item", "payload": row}) for row in records))
            self.assertEqual(edited_paths(latest_task_entries(str(transcript))), [str(target)])

    def test_mechanical_checks_and_safe_skips(self):
        entries = [("USER", "Fix test"), ("TOOL CALL", "exec: pytest -q"),
                   ("TOOL OUTPUT", "[id] exec: pytest -q\nFAILED test_renderer")]
        self.assertTrue(deterministic_findings(entries, "Everything passes", "/tmp"))
        self.assertFalse(deterministic_findings(entries, "test_renderer still fails", "/tmp"))
        doc = [("TOOL CALL", "apply_patch: *** Update File: README.md\n...")]
        self.assertTrue(doc_only_turn(doc, "I adjusted the wording", mimo_audit.DELIVERABLE_RE))
        self.assertFalse(doc_only_turn(doc, "I deployed it", mimo_audit.DELIVERABLE_RE))
        self.assertFalse(doc_only_turn([("USER", "Deploy this README change"), *doc],
                                       "I adjusted the wording", mimo_audit.DELIVERABLE_RE))
        mixed = [("TOOL CALL", "functions.exec: await tools.apply_patch('*** Update File: README.md'); "
                  "await tools.exec_command({cmd: 'git push'})")]
        self.assertFalse(doc_only_turn(mixed, "I adjusted the wording", mimo_audit.DELIVERABLE_RE))
        self.assertEqual(sensitive_paths(["author.py", "src/auth/login.py", "billing.ts", ".env.local", "tls/server.pem"]),
                         ["src/auth/login.py", "billing.ts", ".env.local", "tls/server.pem"])

    def test_finding_id_survives_retry_and_clears_on_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def stop(turn_id: str, message: str, response: str | None):
                payload = {"session_id": "ledger-test", "turn_id": turn_id,
                           "last_assistant_message": message}
                output = io.StringIO()
                with (mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
                      redirect_stdout(output),
                      mock.patch.object(mimo_audit, "STATE_FILE", root / "iterations.json"),
                      mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"),
                      mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"),
                      mock.patch.object(mimo_audit, "record_lesson"),
                      mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value=response) as review):
                    with self.assertRaises(SystemExit):
                        mimo_audit.main()
                return output.getvalue(), review.call_count

            first, calls = stop("1", "I fixed the renderer and tested it.",
                                "VERDICT: ISSUES\n1. Renderer workflow still fails")
            self.assertEqual(calls, 1)
            self.assertIn("[F1]", first)
            second, calls = stop("2", "I fixed the renderer.", None)
            self.assertEqual(calls, 0)
            self.assertIn("[F1]", second)
            third, calls = stop("3", "[F1] fixed: corrected renderer; workflow passed.", "VERDICT: PASS")
            self.assertEqual(calls, 1)
            self.assertEqual(third, "")
            self.assertEqual(json.loads((root / "audit_pending.json").read_text()), {})

    def test_review_outage_escalates_across_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (mock.patch.object(mimo_audit, "STATE_FILE", root / "state.json"),
                  mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"),
                  mock.patch.object(mimo_audit, "ESCALATION_DIR", root / "escalations"),
                  mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"),
                  mock.patch.object(mimo_audit, "report_failure")):
                for turn in range(3):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            mimo_audit.block_audit_error("outage", str(turn), 0, "reviewer unavailable", tmp)
                self.assertEqual(len(list((root / "escalations").glob("*.md"))), 1)
                self.assertEqual(json.loads((root / "verdict.json").read_text())["outage"]["verdict"], "ERROR")

    def test_native_doc_skip_and_sensitive_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "turn.jsonl"
            def stop(path: str, response: str, message: str):
                records = [
                    {"type": "message", "role": "user", "content": "Edit this file"},
                    {"type": "custom_tool_call", "call_id": "edit-1", "name": "functions.exec",
                     "input": f"await tools.apply_patch('*** Update File: {path}\\n')"},
                    {"type": "custom_tool_call_output", "call_id": "edit-1", "output": "patch applied"},
                    {"type": "message", "role": "assistant", "content": message},
                ]
                transcript.write_text("\n".join(json.dumps({"type": "response_item", "payload": row})
                                                for row in records))
                payload = {"session_id": "triage", "turn_id": path, "cwd": tmp,
                           "transcript_path": str(transcript), "last_assistant_message": message}
                output = io.StringIO()
                with (mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
                      redirect_stdout(output),
                      mock.patch.object(mimo_audit, "STATE_FILE", root / "state.json"),
                      mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"),
                      mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"),
                      mock.patch.object(mimo_audit, "record_lesson"),
                      mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value=response) as review):
                    with self.assertRaises(SystemExit):
                        mimo_audit.main()
                return output.getvalue(), review.call_count

            output, calls = stop("README.md", "", "Adjusted the opening sentence.")
            self.assertEqual((output, calls), ("", 0))
            self.assertEqual(json.loads((root / "verdict.json").read_text())["triage"]["verdict"], "SKIP_DOC_ONLY")
            mimo_audit.ledger.register_findings("triage", "VERDICT: ISSUES\n1. Verify the documented command")
            output, calls = stop("README.md", "VERDICT: PASS",
                                 "[F1] fixed: corrected the command and checked its output.")
            self.assertEqual((output, calls), ("", 1))
            output, calls = stop("src/auth/login.py", "VERDICT: PASS\n- Possible authorization bypass",
                                 "Fixed the login flow.")
            self.assertEqual(calls, 1)
            self.assertIn("Possible authorization bypass", output)
            self.assertEqual(json.loads((root / "verdict.json").read_text())["triage"]["verdict"], "ISSUES")


if __name__ == "__main__":
    unittest.main()

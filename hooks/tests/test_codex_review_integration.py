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

    def test_commentary_decisions_are_scoped_to_latest_finding(self):
        from lib import ledger
        def message(role, content, phase=None):
            return {"type": "response_item", "payload": {
                "type": "message", "role": role, "content": content, "phase": phase}}
        review = message("user", "<hook_prompt>VERDICT: ISSUES\n1. [F1] Renderer is broken.\n"
                         "Findings carry IDs. Answer each ID.</hook_prompt>")
        ack = "[F1] fixed: corrected renderer and verified the failing workflow."
        cases = [
            ("commentary", [message("assistant", ack, "commentary")], True),
            ("rejected", [message("assistant", "[F1] rejected: same workflow already passed.", "commentary")], True),
            ("fresh-final-conflict", [message("assistant", ack, "commentary")], True),
            ("later-commentary", [message("assistant", ack, "final_answer"),
                message("assistant", "[F1] rejected: current receipt proves no defect.", "commentary")], True),
            ("unanswered", [message("assistant", "I will look into it.", "commentary")], False),
            ("tool", [{"type": "response_item", "payload": {"type": "function_call_output", "output": ack}}], False),
            ("user", [message("user", ack)], False),
            ("quality-after-ack", [message("assistant", ack, "commentary"),
                message("user", "<hook_prompt>[Repository Quality Gate] Check pending.</hook_prompt>")], True),
            ("reopened", [message("assistant", ack, "commentary"), review], False),
            ("payload-only", [], True),
            ("stale-payload", [message("assistant", ack, "final_answer"), review], False),
        ]
        import random
        rng = random.Random(20261002)
        for trial in range(50):
            sequence, answered = [], False
            for _ in range(rng.randrange(1, 12)):
                event = rng.randrange(4)
                if event == 0:
                    sequence.append(review)
                    answered = False
                elif event == 1:
                    sequence.append(message("assistant", ack, "commentary"))
                    answered = True
                elif event == 2:
                    sequence.append({"type": "response_item", "payload": {
                        "type": "function_call_output", "output": ack}})
                else:
                    sequence.append(message("user", "<hook_prompt>[Quality Gate] Pending.</hook_prompt>"))
            cases.append((f"sequence-{trial}", sequence, answered))
        for name, following, answered in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "transcript.jsonl"
                stale = "I fixed the renderer and tested the workflow."
                rows = [message("user", "Fix and verify the renderer workflow."),
                        message("assistant", stale, "final_answer"),
                        message("assistant", ack, "commentary"), review, *following]
                path.write_text("\n".join(map(json.dumps, rows)))
                payload = {"session_id": name, "turn_id": "retry", "transcript_path": str(path),
                           "last_assistant_message": ack if name in {"payload-only", "stale-payload"} else stale,
                           "cwd": tmp}
                if name == "fresh-final-conflict":
                    payload["last_assistant_message"] = "[F1] rejected: newest final receipt proves no defect."
                if name == "later-commentary":
                    payload["last_assistant_message"] = ack
                if name == "payload-only":
                    rows.pop(2)  # A genuinely new final may not yet be flushed to the transcript.
                    path.write_text("\n".join(map(json.dumps, rows)))
                with (mock.patch.object(mimo_audit, "STATE_FILE", root / "iterations.json"),
                      mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"),
                      mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"),
                      mock.patch.object(ledger, "LEDGER_PATH", root / "audit_pending.json"),
                      mock.patch.object(mimo_audit, "record_lesson"),
                      mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
                      mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO())):
                    ledger.register_findings(name, "VERDICT: ISSUES\n1. Renderer is broken.")
                    with self.assertRaises(SystemExit):
                        mimo_audit.main()
                    self.assertEqual(model.call_count, int(answered))
                    if answered:
                        self.assertIn("[F1]", model.call_args.args[0])
                        if name in {"fresh-final-conflict", "later-commentary"}:
                            self.assertIn("[F1] rejected -", model.call_args.args[0])
                    else:
                        self.assertEqual(ledger.open_findings(name)[0]["id"], "F1")

    def test_max_iteration_release_does_not_block_a_later_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"session_id": "released", "turn_id": "old",
                       "last_assistant_message": "The review remains unresolved."}
            with (mock.patch.object(mimo_audit, "STATE_FILE", root / "iterations.json"),
                  mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"),
                  mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"),
                  mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", root / "audit_pending.json"),
                  mock.patch.object(mimo_audit, "record_lesson")):
                mimo_audit.ledger.register_findings("released", "VERDICT: ISSUES\n1. Old task finding")
                with (mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
                      mock.patch.object(mimo_audit, "load_iterations", return_value=3),
                      redirect_stdout(io.StringIO())):
                    with self.assertRaises(SystemExit):
                        mimo_audit.main()
                self.assertEqual(mimo_audit.ledger.open_findings("released"), [])
                payload["turn_id"] = "new"
                payload["last_assistant_message"] = "I fixed the renderer and tested it."
                with (mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
                      mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as review,
                      redirect_stdout(io.StringIO())):
                    with self.assertRaises(SystemExit):
                        mimo_audit.main()
                review.assert_called_once()

    def test_review_outage_escalates_across_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (mock.patch.object(mimo_audit, "STATE_FILE", root / "state.json"),
                  mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"),
                  mock.patch.object(mimo_audit, "ESCALATION_DIR", root / "escalations"),
                  mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log")):
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

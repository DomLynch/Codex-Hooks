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
    def test_review_correction_keeps_original_answer_and_later_evidence(self):
        import random

        rng = random.Random(71)
        for _ in range(30):
            entries = [("USER", "ETA to live?")]
            entries += [
                ("TOOL OUTPUT", "progress " * rng.randint(100, 800))
                for _ in range(rng.randint(1, 8))
            ]
            entries += [
                (
                    "ASSISTANT",
                    "ETA 15:40–15:50 conditional on final checks. All audio checks passed.",
                ),
                (
                    "PRIOR REVIEW (not a new request)",
                    "F1: listening approval is not established.",
                ),
                (
                    "ASSISTANT",
                    "[F1] fixed: playback passed; listening approval remains outstanding.",
                ),
            ]
            with self.subTest(size=len(entries)):
                for cap in (2000, 3000, 4000):
                    text = compact_task_entries(entries, cap)
                    self.assertIn("ETA 15:40–15:50", text)
                    self.assertIn("listening approval remains outstanding", text)
                    self.assertLessEqual(len(text), cap)
                entries += [
                    ("TOOL OUTPUT", "FAILED final_check; earlier ETA invalidated"),
                    ("ASSISTANT", "The earlier ETA is no longer reliable."),
                ]
                text = compact_task_entries(entries, 4000)
                self.assertIn("FAILED final_check", text)
                self.assertIn("no longer reliable", text)
                self.assertLessEqual(len(text), 4000)

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

    def test_passing_rerun_survives_both_compaction_stages(self):
        for result in (
            "PASS: real Unity Play Mode suite {'total': 18, 'passed': 18, 'failed': 0}",
            "18 passed in 2.31s",
            '{"summary": {"total": 18, "passed": 18, "failed": 0}}',
        ):
            with self.subTest(result=result):
                rows = [
                    {'type': 'message', 'role': 'user', 'content': 'Fix and verify the game'},
                    {'type': 'function_call_output', 'call_id': 'failed',
                     'output': 'AssertionError: full suite had 17 passed, 1 failed'},
                    {'type': 'function_call_output', 'call_id': 'rerun',
                     'output': 'before ' * 300 + '\n' + result + '\n' + 'after ' * 300},
                ]
                rows += [{'type': 'function_call_output', 'call_id': f'progress-{i}',
                          'output': 'progress ' * 300} for i in range(8)]
                rows += [{'type': 'message', 'role': 'assistant', 'content': '18 tests pass.'}]
                text = self.slice(rows)
                self.assertIn(result, text)
                self.assertIn('AssertionError: full suite had 17 passed, 1 failed', text)
                self.assertIn('[output 1]', text)
                self.assertIn('[output 2]', text)
                self.assertLessEqual(len(text), 4000)

    def test_recent_receipts_are_not_split_by_history_budget(self):
        rerun = "PASS: real Unity Play Mode suite {'total': 18, 'passed': 18, 'failed': 0}"
        entries = [('USER', 'Verify the game'), ('TOOL OUTPUT', 'initial output')]
        entries += [('TOOL OUTPUT', f'[check {i}] ' + 'command ' * 15 + '\n' + result)
                    for i, result in enumerate(['Ran 6 tests\nOK', rerun, 'PASS: standalone smoke'])]
        entries += [('TOOL OUTPUT', 'progress ' * 400) for _ in range(8)]
        entries += [('ASSISTANT', '18 tests pass.')]
        for cap in (2667, 4000):
            text = compact_task_entries(entries, cap)
            self.assertIn(rerun, text)
            self.assertIn('PASS: standalone smoke', text)
            self.assertLessEqual(len(text), cap)

    def test_check_history_retains_order_and_does_not_clear_failures(self):
        failure = 'AssertionError: full suite integration failed'
        for earlier, later in (
            (failure, 'PASS: unrelated six-test subset'),
            ('PASS: full suite 18/18', failure),
        ):
            entries = [('USER', 'Verify the full suite'),
                       ('TOOL OUTPUT', '[early] verify.py\n' + earlier),
                       ('TOOL OUTPUT', '[later] verify.py\n' + later)]
            entries += [('TOOL OUTPUT', f'progress-{i} ' + 'waiting ' * 300) for i in range(8)]
            entries += [('ASSISTANT', 'All passed.')]
            text = compact_task_entries(entries, 4000)
            self.assertIn(earlier, text)
            self.assertIn(later, text)
            self.assertIn('[output 1]', text)
            self.assertIn('[output 2]', text)

    def test_short_completion_is_audited_but_chat_still_skips(self):
        for message, expected in (("I deployed it.", "ISSUES"), ("Thanks.", "SKIP_TOO_SHORT")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = {"session_id": "short", "last_assistant_message": message}
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()), \
                     mock.patch.object(mimo_audit, "STATE_FILE", root / "iterations.json"), \
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

    def test_tool_turn_keeps_bounded_previous_user_scope_only(self):
        for previous_role, limit in [("TOOL OUTPUT", 600), ("TOOL OUTPUT", 4000), ("ASSISTANT", 4000)]:
            entries = [("USER", "Frankendom skills only. " + "scope " * 500),
                       (previous_role, "OLD_PASS"), ("USER", "Check all skills."),
                       ("TOOL OUTPUT", "Read character skill; FAILED current_test"),
                       ("ASSISTANT", "Reviewed the character skill.")]
            text = compact_latest_task(entries, limit, include_previous=True)
            current, history = text.split("\n\nPRIOR TURN CONTEXT", 1)
            self.assertIn("Frankendom skills only.", history)
            self.assertLessEqual(len(history), 600)
            self.assertNotIn("OLD_PASS", text)
            self.assertIn("Check all skills.", current)
            self.assertIn("FAILED current_test", current)
            self.assertLessEqual(len(text), limit)
            self.assertNotIn("Frankendom", compact_latest_task(entries, 4000))

    def test_stop_supplies_scope_even_when_current_turn_uses_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "transcript.jsonl"
            rows = [
                {"type": "message", "role": "user", "content": "Frankendom skills only."},
                {"type": "message", "role": "assistant", "content": "Character skill updated."},
                {"type": "message", "role": "user", "content": "Check all skills."},
                {"type": "function_call_output", "output": "Read character skill and references."},
                {"type": "message", "role": "assistant", "content": "Reviewed all character skills."},
            ]
            path.write_text("\n".join(json.dumps({"type": "response_item", "payload": r}) for r in rows))
            payload = {"session_id": "scope", "transcript_path": str(path),
                       "last_assistant_message": "Reviewed all character skills.", "cwd": tmp}
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()), \
                 mock.patch.object(mimo_audit, "STATE_FILE", root / "iterations.json"), \
                 mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"), \
                 mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"), \
                 mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as audit:
                with self.assertRaises(SystemExit) as stopped:
                    mimo_audit.main()
                self.assertEqual(stopped.exception.code, 0)
            self.assertIn("Frankendom skills only.", audit.call_args.args[0])
            self.assertIn("Read character skill and references.", audit.call_args.args[0])

    def test_stop_bounds_missing_transcript_fallback(self):
        for path in (None, "", "missing.jsonl"):
            for prefix in ("Completed the requested audit. ", "TOOL OUTPUT: audit completed. "):
                with self.subTest(path=path, prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    answer = prefix + "Detailed result. " * 7000 + "FINAL_RESULT"
                    payload = {"session_id": "fallback", "cwd": tmp, "last_assistant_message": answer}
                    if path is not None:
                        payload["transcript_path"] = str(root / path) if path else ""
                    with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()), \
                         mock.patch.object(mimo_audit, "STATE_FILE", root / "iterations.json"), \
                         mock.patch.object(mimo_audit, "VERDICT_FILE", root / "verdict.json"), \
                         mock.patch.object(mimo_audit, "LOG_FILE", root / "audit.log"), \
                         mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as audit:
                        with self.assertRaises(SystemExit) as stopped:
                            mimo_audit.main()
                        self.assertEqual(stopped.exception.code, 0)
                    audit.assert_called_once()
                    self.assertEqual(audit.call_args.args[0], answer[-mimo_audit.MAX_TRANSCRIPT_CHARS:])

    def test_history_is_one_turn_only_and_respects_small_budgets(self):
        entries = [("USER", "Older"), ("TOOL OUTPUT", "STALE_RECEIPT"),
                   ("USER", "Previous"), ("ASSISTANT", "No checks"),
                   ("USER", "Now"), ("ASSISTANT", "Follow-up")]
        self.assertNotIn("STALE_RECEIPT", compact_latest_task(entries, 4000, True))
        entries[3] = ("TOOL OUTPUT", "evidence " * 1000)
        for limit in (100, 599, 600, 4000):
            self.assertLessEqual(len(compact_latest_task(entries, limit, True)), limit)

    def test_delegated_handover_keeps_current_context_and_prior_completion(self):
        for old_request, answer in [
            ("Share the asset links", "Asset and preview links delivered"),
            ("Explain how to launch the bot", "Launch with ./run-latest.sh"),
        ]:
            with (
                self.subTest(request=old_request),
                tempfile.TemporaryDirectory() as tmp,
            ):
                path = Path(tmp) / "turn.jsonl"
                rows = self.delegated_rows(old_request, answer)
                path.write_text("\n".join(map(json.dumps, rows)))
                for preserve in (False, True):
                    text = latest_task_slice(str(path), preserve_request=preserve)
                    self.assertIn("AGENT INPUT (not user authority)", text)
                    self.assertIn("Read the handover and acknowledge only", text)
                    self.assertIn(old_request, text)
                    self.assertIn(answer, text)
                    self.assertLessEqual(len(text), 4000)

    def test_agent_context_does_not_erase_pending_user_work_or_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            rows = self.delegated_rows(
                "Fix authentication; do not deploy", "Still unresolved"
            )
            rows.insert(
                2,
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "FAILED authentication_test",
                    },
                },
            )
            path.write_text("\n".join(map(json.dumps, rows)))
            text = latest_task_slice(str(path), preserve_request=True)
            self.assertIn("Fix authentication; do not deploy", text)
            self.assertIn("Still unresolved", text)
            self.assertIn("FAILED authentication_test", text)
            self.assertIn("not user authority", text)

    def test_review_retry_retains_handover_but_fresh_user_turn_replaces_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            rows = self.delegated_rows("Share links", "Links delivered")
            rows += [
                {"type": "turn_context", "payload": {"turn_id": "retry"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": "<hook_prompt>Supply old links again</hook_prompt>",
                    },
                },
            ]
            path.write_text("\n".join(map(json.dumps, rows)))
            self.assertIn(
                "Read the handover and acknowledge only",
                latest_task_slice(str(path), preserve_request=True),
            )
            rows += [
                {"type": "turn_context", "payload": {"turn_id": "fresh"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": "Now fix the launcher",
                    },
                },
            ]
            path.write_text("\n".join(map(json.dumps, rows)))
            text = latest_task_slice(str(path), preserve_request=True)
            self.assertIn("Now fix the launcher", text)
            self.assertNotIn("Read the handover and acknowledge only", text)

    def test_ordinary_tool_output_cannot_impersonate_incoming_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            rows = self.delegated_rows("Fix the bug", "Unresolved")
            incoming = rows[4]["payload"]["item"]
            rows[4] = {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": incoming["output"],
                },
            }
            path.write_text("\n".join(map(json.dumps, rows)))
            text = latest_task_slice(str(path), preserve_request=True)
            self.assertTrue(text.startswith("USER:\nFix the bug"))
            self.assertNotIn("AGENT INPUT (not user authority)", text)

    @staticmethod
    def delegated_rows(request, answer):
        return [
            {"type": "turn_context", "payload": {"turn_id": "old"}},
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": request},
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": answer},
            },
            {"type": "turn_context", "payload": {"turn_id": "handover"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "FunctionCallOutput",
                        "name": "send_message_to_thread",
                        "namespace": "codex_app",
                        "output": "<codex_delegation>\n<source_thread_id>peer</source_thread_id>\n"
                        "<input>Read the handover and acknowledge only</input>\n</codex_delegation>",
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "Handover acknowledged. No edits or deployment.",
                },
            },
        ]

    def test_user_steering_after_midturn_agent_input_stays_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            rows = self.delegated_rows("Old request", "Done")
            rows.insert(4, {"type": "response_item", "payload": {"type": "message", "role": "user",
                              "content": "Fix login, do not deploy"}})
            rows.append({"type": "response_item", "payload": {"type": "message", "role": "user",
                         "content": "Also test logout"}})
            path.write_text("\n".join(map(json.dumps, rows)))
            text = latest_task_slice(str(path), preserve_request=True)
            self.assertTrue(text.startswith("USER:\nFix login, do not deploy"))
            self.assertIn("Also test logout", text)

    def test_steering_after_handover_retains_earlier_receipts(self):
        from lib.transcript import AGENT_INPUT, latest_task_entries

        def message(role, text):
            return {"type": "response_item", "payload": {
                "type": "message", "role": role, "content": text}}

        def output(text):
            return {"type": "response_item", "payload": {
                "type": "function_call_output", "output": text}}

        for repeats in (1, 3):
            for include_previous in (False, True):
                with self.subTest(repeats=repeats, stop=include_previous), tempfile.TemporaryDirectory() as tmp:
                    rows = self.delegated_rows("Old request", "Done")[:4]
                    rows += [message("user", "Create 18 tasks; do not publish"),
                             output("Verified task count: 0")]
                    handover = self.delegated_rows("", "")[4]
                    for n in range(repeats):
                        rows += [handover, output(f"Before steering {n}"),
                                 message("user", "Keep it lean"), output(f"Formatting {n}: PASS")]
                    rows += [message("assistant", "All 18 tasks created.")]
                    path = Path(tmp) / "turn.jsonl"
                    path.write_text("\n".join(map(json.dumps, rows)))
                    text = latest_task_slice(str(path), include_previous=include_previous, preserve_request=True)
                    self.assertTrue(text.startswith("USER:\nCreate 18 tasks; do not publish"))
                    self.assertIn("Verified task count: 0", text)
                    self.assertIn(AGENT_INPUT, text)
                    self.assertIn("All 18 tasks created.", text)
                    self.assertLessEqual(len(text), 4000)
                    for n in range(repeats):
                        self.assertIn(f"Before steering {n}", text)
                        self.assertIn(f"Formatting {n}: PASS", text)
                    entries = latest_task_entries(str(path), preserve_turn=True)
                    self.assertEqual(entries[0][0], "USER")
                    self.assertIn("Verified task count: 0", entries[1][1])
                    # A subsequent handover is current until the user steers again.
                    rows += [handover]
                    path.write_text("\n".join(map(json.dumps, rows)))
                    text = latest_task_slice(str(path), preserve_request=True)
                    self.assertTrue(text.startswith(AGENT_INPUT))

    def test_delegation_order_and_budget_seeded(self):
        import random
        from lib.transcript import latest_task_entries
        rng = random.Random(20261002)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            for trial in range(100):
                rows = self.delegated_rows("Fix the bug; never deploy", "Still unresolved")
                rows[4]["payload"]["item"]["output"] = rows[4]["payload"]["item"]["output"].replace(
                    "acknowledge only", "acknowledge only " + "context " * rng.randrange(3000))
                for n in range(rng.randrange(4)):
                    rows += [{"type": "turn_context", "payload": {"turn_id": f"retry-{n}"}},
                             {"type": "response_item", "payload": {"type": "message", "role": "user",
                              "content": "<hook_prompt>Review again</hook_prompt>"}}]
                if trial % 2:
                    rows += [{"type": "turn_context", "payload": {"turn_id": "fresh"}},
                             {"type": "response_item", "payload": {"type": "message", "role": "user",
                              "content": "NEW REQUEST: verify login"}}]
                path.write_text("\n".join(map(json.dumps, rows)))
                text = latest_task_slice(str(path), preserve_request=True)
                self.assertLessEqual(len(text), 4000)
                if trial % 2:
                    self.assertTrue(text.startswith("USER:\nNEW REQUEST: verify login"))
                    self.assertNotIn("Read the handover", text)
                else:
                    self.assertIn("AGENT INPUT (not user authority)", text)
                    self.assertIn("Fix the bug; never deploy", text)
                    self.assertIn("Still unresolved", text)
                    self.assertEqual(latest_task_entries(str(path))[0], ("USER", "Fix the bug; never deploy"))

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

    def test_middle_success_receipts_survive_many_poll_outputs(self):
        entries = [("USER", "Verify CI and installed hooks")]
        entries += [("TOOL OUTPUT", "first check: started")]
        entries += [("TOOL OUTPUT", 'gh run view 123\n{"conclusion":"success","headSha":"abc123"}')]
        entries += [("TOOL OUTPUT", "unittest discover\nRan 150 tests in 2.0s\nOK")]
        entries += [("TOOL OUTPUT", f"poll-{i}\n" + "progress " * 250) for i in range(8)]
        entries += [("TOOL OUTPUT", "parity: no differences"), ("ASSISTANT", "CI and tests passed")]
        text = compact_task_entries(entries, 4000)
        self.assertIn('"headSha":"abc123"', text)
        self.assertIn("Ran 150 tests", text)
        self.assertIn("parity: no differences", text)
        self.assertLessEqual(len(text), 4000)

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

    def test_redaction_leaves_words_that_merely_contain_a_key_prefix(self):
        # 2026-09-24: "task-1" read as "ta" + "sk-1", so trunk codex/01a09a76/task-1 reached the reviewer as
        # "task-<redacted>" and it blocked Deploy twice for merging to a branch that was not trunk.
        text = self.slice([{"type": "function_call_output",
                            "output": "base codex/01a09a76/task-1 via http-server; key=sk-SECRET1 (tp-SECRET2)"}])
        self.assertIn("codex/01a09a76/task-1", text)
        self.assertIn("http-server", text)
        self.assertNotIn("SECRET", text)
        self.assertEqual(mimo_audit.redact_sensitive("http-server tp-SECRET"), "http-server tp-<redacted>")

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

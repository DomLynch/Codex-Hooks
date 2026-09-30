from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import mimo_audit  # noqa: E402
from lib import audit_state  # noqa: E402
import session_start  # noqa: E402
from lib import policies  # noqa: E402
from lib import log as hook_log  # noqa: E402


class CompletionTests(unittest.TestCase):
    def invoke(self, user, answer, verdict="PASS", exhausted=None, stop_review="1", pending=False):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            rows = []
            for role, text in (("user", user), ("assistant", answer)):
                if hasattr(mimo_audit, "latest_task_slice_claude"):
                    rows.append({"type": role, "message": {"content": text}})
                else:
                    rows.append({"type": "response_item", "payload":
                                 {"type": "message", "role": role, "content": text}})
            path = root / "transcript.jsonl"
            path.write_text("\n".join(map(json.dumps, rows)))
            payload = {"session_id": "completion", "transcript_path": str(path)}
            stdout, stderr = io.StringIO(), io.StringIO()
            stack.enter_context(mock.patch("sys.stdin", io.StringIO(json.dumps(payload))))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            for module, name, value in ((audit_state, "STATE_FILE", root / "iterations.json"),
                                        (audit_state, "VERDICT_FILE", root / "verdict.json"),
                                        (mimo_audit, "LOG_FILE", root / "audit.log")):
                stack.enter_context(mock.patch.object(module, name, value))
            stack.enter_context(mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", root / "ledger.json"))
            if pending:
                mimo_audit.ledger.register_findings("completion", "VERDICT: ISSUES\n1. Old task finding")
            stack.enter_context(mock.patch.object(mimo_audit, "record_lesson"))
            stack.enter_context(mock.patch.object(mimo_audit, "hook_env", lambda name, default="":
                                                  stop_review if name == "CLAUDE_STOP_REVIEW" else default))
            if exhausted == "iterations":
                stack.enter_context(mock.patch.object(mimo_audit, "load_iterations", return_value=3))
            elif exhausted == "damper":
                stack.enter_context(mock.patch.object(mimo_audit, "increment_consecutive_blocks", return_value=3))
            transport = stack.enter_context(mock.patch.object(
                mimo_audit, "call_mimo", return_value=f"VERDICT: {verdict}\n1. Continue the authorized fix." if verdict == "ISSUES" else "VERDICT: PASS"))
            with self.assertRaises(SystemExit) as exit:
                mimo_audit.main()
            self.assertEqual(exit.exception.code, 0)
            state = json.loads((root / "verdict.json").read_text())["completion"]["verdict"]
            self.last_open_findings = mimo_audit.ledger.open_findings("completion")
            return state, stdout.getvalue(), stderr.getvalue(), transport

    def test_stop_review_off_switch_skips_the_model_without_passing(self):
        state, _, _, transport = self.invoke("Fix the typo.", "Fixed the typo in README.", stop_review="0")
        transport.assert_not_called()
        self.assertEqual(state, "SKIP_DISABLED", "a verdict is written so the orchestrator does not wait, but not PASS")
        state, _, _, transport = self.invoke("Fix the typo.", "Fixed the typo in README.")
        transport.assert_called()

    def test_short_handoffs_reach_reviewer_and_block(self):
        for answer in ("Shall I correct the typo?", "Would you like me to proceed?",
                       "I can fix the typo.", "The work is not yet complete.",
                       "May I correct it?", "I can correct it if you like.",
                       "Next: replace teh with the.",
                       "The release is not yet fully deployed.",
                       "The remaining work is running the approved script."):
            with self.subTest(answer=answer):
                state, out, _, transport = self.invoke("Apply the approved fix now.", answer, "ISSUES")
                self.assertEqual(state, "ISSUES")
                self.assertEqual(json.loads(out)["decision"], "block")
                prompt = transport.call_args.args[0]
                self.assertIn(answer, prompt)
                self.assertIn("An honest unfinished summary is still premature stopping", prompt)
                self.assertIn("Explicit stop/pause", prompt)

    def test_reviewer_can_allow_legitimate_pause(self):
        state, out, _, transport = self.invoke("Stop now; do not deploy.",
                                              "The release is not yet fully deployed.")
        transport.assert_called_once()
        self.assertEqual((state, out), ("PASS", ""))

    def test_request_variations_cannot_hide_behind_chat_skip(self):
        for user, answer in (
            ("Change teh to the in README.md now.", "May I correct it?"),
            ("Rename foo to bar in app.py now.", "I can do that if you like."),
            ("Update README.md now.", "Next: replace teh with the."),
        ):
            with self.subTest(user=user):
                state, out, _, transport = self.invoke(user, answer, "ISSUES")
                transport.assert_called_once()
                self.assertEqual(state, "ISSUES")
                self.assertEqual(json.loads(out)["decision"], "block")

    def test_ordinary_chat_still_skips(self):
        state, out, _, transport = self.invoke("Thanks.", "You're welcome.")
        transport.assert_not_called()
        self.assertEqual((state, out), ("SKIP_TOO_SHORT", ""))

    def test_exhaustion_is_visible_not_pass_or_infinite_loop(self):
        for limit, expected in (("iterations", "MAX_ALLOW"), ("damper", "DAMPER_ALLOW")):
            with self.subTest(limit=limit):
                state, out, err, transport = self.invoke("Fix the typo.", "Shall I fix it?", "ISSUES", limit)
                self.assertEqual((state, out), (expected, ""))
                self.assertIn("without PASS", err)
                self.assertIn("not completion", err)
                self.assertEqual(transport.call_count, int(limit == "damper"))

    def test_iteration_release_clears_old_findings(self):
        self.invoke("Fix the typo.", "Still unresolved.", exhausted="iterations", pending=True)
        self.assertEqual(self.last_open_findings, [])

    def test_both_injection_surfaces_preserve_authorization_and_scope(self):
        for policy in (session_start.SESSION_BASELINE, policies.AUTONOMY_POLICY):
            self.assertIn("do not ask for the same permission twice", policy)
            self.assertIn("Explicit stop/pause", policy)
            self.assertIn("unrelated backlog tasks", policy)
            self.assertIn("retry exhaustion is unresolved work, not success", policy.lower())

    def test_release_closeout_shared_between_injection_surfaces(self):
        policy = policies.COMPLETION_POLICY
        self.assertIn(policy, session_start.SESSION_BASELINE)
        self.assertIn(policy, policies.AUTONOMY_POLICY)
        for phrase in ('completion_commands', 'same check/workflow', 'weakened assertions',
                       'status-only', 'blocker', 'older Sentry'):
            self.assertIn(phrase, policy)
        self.assertIn(policies.SENTRY_POLICY, session_start.SESSION_BASELINE)
        self.assertIn(policies.SENTRY_POLICY, policies.WORK_QUALITY_POLICY)
        policy = policies.RELEASE_POLICY
        self.assertIn(policy, session_start.SESSION_BASELINE)
        self.assertIn(policy, policies.AUTONOMY_POLICY)
        for requirement in ("configured deployment workflow", "git status --porcelain",
                            "no-deploy", "unrelated work", "empty commit"):
            self.assertIn(requirement, policy)
        self.assertIn("release close-out by default", mimo_audit.AUDIT_PROMPT)
        self.assertIn("real ownership/access blocker", mimo_audit.AUDIT_PROMPT)

    def test_audit_outage_reports_metadata_and_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "state.json"), \
                mock.patch.object(mimo_audit, "log"), \
                mock.patch.object(mimo_audit, "report_failure") as report, \
                mock.patch.object(mimo_audit, "save_iterations"), \
                mock.patch.object(mimo_audit, "save_verdict") as verdict, \
                mock.patch.object(mimo_audit, "block_stop") as block:
            mimo_audit.block_audit_error("session", "turn", 0, "private provider details")
        report.assert_called_once_with("mimo_audit", "audit_unavailable")
        self.assertEqual(verdict.call_args.args[2], "ERROR")
        block.assert_called_once()

    def test_an_account_error_releases_at_once_instead_of_blocking(self):
        # 2026-09-30: the reviewer account hit its weekly limit; each Stop blocked twice before release, in every
        # lane, for a limit five days from resetting. A retry cannot fix quota or login, so release immediately.
        for message in ("audit API error: Claude audit failed (1): You've hit your weekly limit · resets Oct 5",
                        "audit API error: Failed to authenticate: OAuth session expired and could not be refreshed"):
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "state.json"), \
                    mock.patch.object(mimo_audit, "log"), \
                    mock.patch.object(mimo_audit, "report_failure"), \
                    mock.patch.object(mimo_audit, "save_iterations"), \
                    mock.patch.object(mimo_audit, "save_verdict") as verdict, \
                    mock.patch.object(mimo_audit, "block_stop") as block, \
                    mock.patch.object(mimo_audit, "allow_stop") as allow:
                mimo_audit.block_audit_error("session", "turn", 0, message)
            block.assert_not_called()
            allow.assert_called_once()
            self.assertTrue(allow.call_args.kwargs["unresolved"])
            self.assertEqual(verdict.call_args.args[2], "ERROR")   # still recorded, never a PASS
        # a timeout is not an account error: it still blocks and retries
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "state.json"), \
                mock.patch.object(mimo_audit, "log"), mock.patch.object(mimo_audit, "report_failure"), \
                mock.patch.object(mimo_audit, "save_iterations"), mock.patch.object(mimo_audit, "save_verdict"), \
                mock.patch.object(mimo_audit, "block_stop") as block:
            mimo_audit.block_audit_error("session", "turn", 0, "audit API error: Claude audit timed out after 100s")
        block.assert_called_once()

    def test_log_reports_hook_exceptions_not_expected_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hook_log, "LOG_DIR", Path(tmp)), \
                mock.patch.object(hook_log, "report_failure") as report:
            for message in ("quality files=1 ok=False", "VERDICT: ISSUES", "ALLOW: chat turn"):
                hook_log.log("post_tool", message)
            report.assert_not_called()
            hook_log.log("post_tool", "quality error: private path and secret")
            report.assert_called_once_with("post_tool", "hook_exception")

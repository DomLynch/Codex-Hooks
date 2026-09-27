"""Deterministic Stop-hook gates: failed checks, conflict markers, sensitive/doc-only triage, peer-started actions, live claims."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import mimo_audit  # noqa: E402
from lib import audit_state  # noqa: E402





class UnacknowledgedFailureGateTests(unittest.TestCase):
    """Deterministic pre-gate: failed check runner + silent summary → block without a model call."""

    RED = "[c1] Bash: python -m pytest tests/\n\nFAILED tests/test_x.py::test_y - AssertionError: 1 != 2\n1 failed, 3 passed"
    GREEN = "[c2] Bash: python -m pytest tests/\n\n4 passed in 0.3s"

    def test_fires_when_last_test_run_failed_and_summary_is_silent(self) -> None:
        from lib.transcript import unacknowledged_check_failure as gate

        entries = [("USER", "fix it"), ("TOOL OUTPUT", self.RED), ("ASSISTANT", "Done. Implemented the fix and pushed.")]
        out = gate(entries, "Done. Implemented the fix and pushed.")
        self.assertIn("AssertionError", out)

    def test_silent_when_summary_acknowledges_the_failure(self) -> None:
        from lib.transcript import unacknowledged_check_failure as gate

        entries = [("TOOL OUTPUT", self.RED)]
        self.assertEqual(gate(entries, "test_y is still failing; it needs the fixture from PR 12."), "")
        self.assertEqual(gate(entries, "One regression remains, documented below."), "")

    def test_silent_when_a_later_rerun_is_green(self) -> None:
        from lib.transcript import unacknowledged_check_failure as gate

        entries = [("TOOL OUTPUT", self.RED), ("TOOL CALL", "Edit: fix"), ("TOOL OUTPUT", self.GREEN)]
        self.assertEqual(gate(entries, "All green now."), "")

    def test_a_green_run_of_a_different_check_does_not_clear_a_failed_one(self) -> None:
        # Audit B: pytest FAILED, then an unrelated `ruff check` passed -> the failure was lost.
        from lib.transcript import unacknowledged_check_failure as gate

        lint = "[c5] Bash: ruff check .\n\nAll checks passed!"
        self.assertIn("AssertionError", gate([("TOOL OUTPUT", self.RED), ("TOOL OUTPUT", lint)], "Done; everything passes."))
        self.assertEqual(gate([("TOOL OUTPUT", self.RED), ("TOOL OUTPUT", lint), ("TOOL OUTPUT", self.GREEN)], "Done."), "")

    def test_a_chained_green_run_clears_each_check_it_reran(self) -> None:
        from lib.transcript import unacknowledged_check_failure as gate

        both = "[c6] Bash: pytest -q && ruff check .\n\n4 passed\nAll checks passed!"
        self.assertEqual(gate([("TOOL OUTPUT", self.RED), ("TOOL OUTPUT", both)], "Done."), "")

    def test_ignores_error_strings_from_non_test_tools(self) -> None:
        from lib.transcript import unacknowledged_check_failure as gate

        grep = "[c3] Bash: grep -rn ERROR src/\n\nsrc/log.py:12: log.error('ERROR: boom')\nsrc/x.py:5: raise Error('fail')"
        self.assertEqual(gate([("TOOL OUTPUT", grep)], "Found the two log sites."), "")

    def test_silent_when_no_test_runner_ran(self) -> None:
        from lib.transcript import unacknowledged_check_failure as gate

        self.assertEqual(gate([("TOOL OUTPUT", "[c4] Bash: ls\n\na.py b.py")], "Listed files."), "")

    def test_main_blocks_via_gate_without_calling_the_model(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "ledger.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[("TOOL OUTPUT", self.RED)]),
            ):
                payload = {"session_id": "g", "turn_id": "t1", "transcript_path": "/nope", "last_assistant_message": "Done, shipped."}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    buf = io.StringIO()
                    with redirect_stdout(buf), self.assertRaises(SystemExit):
                        mimo_audit.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(out["decision"], "block")
        self.assertIn("does not mention it", out["reason"])
        self.assertIn("AssertionError", out["reason"])
        model.assert_not_called()


class ConflictMarkerGateTests(unittest.TestCase):
    CONFLICT = "a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\nb\n"

    def _edit_call(self, path: str) -> tuple[str, str]:
        return ("TOOL CALL", f'Edit: [t1] {{"file_path": "{path}", "old_string": "x", "new_string": "y"}}')

    def test_fires_on_edited_file_with_markers(self) -> None:
        from lib.transcript import conflict_marker_files, edited_paths

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "spec.md"
            f.write_text(self.CONFLICT)
            hits = conflict_marker_files(edited_paths([self._edit_call(str(f))]))
        self.assertEqual(hits, [str(f)])

    def test_silent_when_resolved(self) -> None:
        from lib.transcript import conflict_marker_files, edited_paths

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "spec.md"
            f.write_text("a\nours\nb\n")
            self.assertEqual(conflict_marker_files(edited_paths([self._edit_call(str(f))])), [])

    def test_silent_on_markdown_rule_and_inline_chevrons(self) -> None:
        from lib.transcript import conflict_marker_files

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "doc.md"
            f.write_text("Title\n=======\nbody with <<<<<<< inline text\n")
            self.assertEqual(conflict_marker_files([str(f)]), [], "=== underline and mid-line chevrons are not conflicts")

    def test_ignores_conflicted_files_not_edited_this_turn(self) -> None:
        from lib.transcript import conflict_marker_files, edited_paths

        with tempfile.TemporaryDirectory() as tmp:
            theirs = Path(tmp) / "theirs.md"
            theirs.write_text(self.CONFLICT)
            mine = Path(tmp) / "mine.py"
            mine.write_text("ok\n")
            self.assertEqual(conflict_marker_files(edited_paths([self._edit_call(str(mine))])), [],
                             "another agent's conflict in the same tree must not block this session")


class DeterministicFindingsTests(unittest.TestCase):
    def test_file_mention_alone_does_not_block(self) -> None:
        from lib.transcript import deterministic_findings

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "src").mkdir()
            (Path(tmp) / "src" / "loot.ts").write_text("export const loot = 1;\n")
            msg = "The change is in src/loot.ts."
            self.assertEqual(deterministic_findings([], msg, tmp), [])

    def test_multiple_gates_combine_and_main_numbers_them(self) -> None:
        from lib.transcript import deterministic_findings

        with tempfile.TemporaryDirectory() as tmp:
            conflicted = Path(tmp) / "spec.md"
            conflicted.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n")
            (Path(tmp) / "shot.png").write_bytes(b"\x89PNG")
            red = "[c1] Bash: python -m pytest\n\nFAILED t.py::t - AssertionError\n1 failed"
            entries = [
                ("TOOL CALL", f'Edit: [t1] {{"file_path": "{conflicted}", "old_string": "a", "new_string": "b"}}'),
                ("TOOL OUTPUT", red),
            ]
            msg = "Done. shot.png confirms the layout."
            self.assertEqual(len(deterministic_findings(entries, msg, tmp)), 2)

            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "ledger.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=entries),
            ):
                payload = {"session_id": "d", "turn_id": "t", "transcript_path": "/nope", "last_assistant_message": msg, "cwd": tmp}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    buf = io.StringIO()
                    with redirect_stdout(buf), self.assertRaises(SystemExit):
                        mimo_audit.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(out["decision"], "block")
        for marker in ("1. [F1] A test/check run", "2. [F2] Files you edited", "[F1] fixed:"):
            self.assertIn(marker, out["reason"])
        model.assert_not_called()


class SensitivePathTriageTests(unittest.TestCase):
    def _edit(self, path):
        return ("TOOL CALL", f'Edit: [t] {{"file_path": "{path}", "old_string": "a", "new_string": "b"}}')

    def test_detects_sensitive_paths_and_ignores_others(self) -> None:
        from lib.transcript import sensitive_paths

        entries = [self._edit("src/auth/login.py"), self._edit("README.md"), self._edit("lib/payment/stripe_client.ts"), self._edit("src/ui/button.tsx")]
        self.assertEqual(sensitive_paths(entries), ["src/auth/login.py", "lib/payment/stripe_client.ts"])

    def test_pass_warnings_extracts_bullets_and_numbers_only(self) -> None:
        from lib.transcript import pass_warnings

        self.assertEqual(pass_warnings("VERDICT: PASS\nLooks fine overall."), [], "prose is not a warning")
        self.assertEqual(pass_warnings("VERDICT: PASS\nWarnings:\n- token logged at debug level\n2) no rate limit on /login"),
                         ["token logged at debug level", "no rate limit on /login"])
        self.assertEqual(pass_warnings("VERDICT: ISSUES\n1. x"), [], "only PASS verdicts have warnings")

    def _run(self, mimo_audit, tmp, entries, verdict):
        with (
            mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
            mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
            mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
            mock.patch.object(mimo_audit, "log") as log,
            mock.patch.object(mimo_audit, "record_lesson"),
            mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value=verdict) as model,
            mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
            mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=entries),
        ):
            payload = {"session_id": "S", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp, "last_assistant_message": "Done."}
            with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                buf = io.StringIO()
                with redirect_stdout(buf), self.assertRaises(SystemExit):
                    mimo_audit.main()
        out = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return out, model, [c.args[0] for c in log.call_args_list]

    def test_sensitive_edit_upgrades_pass_with_warnings_to_block(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            out, model, logs = self._run(mimo_audit, tmp, [self._edit("api/auth.py")],
                                         "VERDICT: PASS\n- session token compared with == not hmac.compare_digest")
        self.assertEqual(out["decision"], "block")
        self.assertIn("[F1] session token compared", out["reason"])
        self.assertIn("SENSITIVE PATHS EDITED", model.call_args.args[0], "reviewer was told")
        self.assertTrue(any("SENSITIVE_UPGRADE" in m for m in logs))

    def test_sensitive_edit_clean_pass_still_passes(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = self._run(mimo_audit, tmp, [self._edit("api/auth.py")], "VERDICT: PASS")
        self.assertEqual(out, {}, "no warnings -> allow")

    def test_non_sensitive_edit_keeps_pass_with_warnings(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            out, model, _ = self._run(mimo_audit, tmp, [self._edit("docs/guide.md")],
                                      "VERDICT: PASS\n- consider a table of contents")
        self.assertEqual(out, {}, "warnings on ordinary paths stay advisory")
        self.assertNotIn("SENSITIVE PATHS", model.call_args.args[0])


class DocOnlyTriageTests(unittest.TestCase):
    def _edit(self, path):
        return ("TOOL CALL", f'Edit: [t] {{"file_path": "{path}", "old_string": "a", "new_string": "b"}}')

    def _bash(self, cmd):
        return ("TOOL CALL", f"Bash: [b] {cmd}")

    def test_doc_only_with_readonly_bash_and_no_claim(self) -> None:
        from lib.transcript import doc_only_turn

        entries = [self._edit("README.md"), self._edit("docs/guide.rst"), self._bash("git status"), self._bash("ls docs")]
        self.assertTrue(doc_only_turn(entries, "Updated the install docs.", mimo_audit.DELIVERABLE_RE))

    def test_disqualified_by_code_edit(self) -> None:
        from lib.transcript import doc_only_turn

        self.assertFalse(doc_only_turn([self._edit("README.md"), self._edit("src/x.py")], "Updated docs.", mimo_audit.DELIVERABLE_RE))

    def test_disqualified_by_mutating_bash(self) -> None:
        from lib.transcript import doc_only_turn

        for cmd in ("sed -i 's/a/b/' src/x.py", "git push origin main", "python3 build.py", "echo x > out.txt", "npm run build"):
            self.assertFalse(doc_only_turn([self._edit("README.md"), self._bash(cmd)], "Updated docs.", mimo_audit.DELIVERABLE_RE), cmd)

    def test_disqualified_by_deliverable_claim(self) -> None:
        from lib.transcript import doc_only_turn

        self.assertFalse(doc_only_turn([self._edit("README.md")], "Updated docs and pushed to main.", mimo_audit.DELIVERABLE_RE))

    def test_disqualified_when_nothing_edited(self) -> None:
        from lib.transcript import doc_only_turn

        self.assertFalse(doc_only_turn([self._bash("ls")], "Listed files.", mimo_audit.DELIVERABLE_RE))

    def test_main_skips_model_on_doc_only_but_gates_still_run_first(self) -> None:

        red = "[c1] Bash: python -m pytest\n\nFAILED t.py::t - AssertionError\n1 failed"
        for entries, expect_model, expect_block in (
            ([self._edit("README.md")], False, False),                       # pure doc turn -> skip, allow
            ([self._edit("README.md"), ("TOOL OUTPUT", red)], False, True),  # doc turn but red test unmentioned -> gate blocks first
        ):
            with tempfile.TemporaryDirectory() as tmp:
                with (
                    mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                    mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                    mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
                    mock.patch.object(mimo_audit, "log"),
                    mock.patch.object(mimo_audit, "record_lesson"),
                    mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
                    mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                    mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=entries),
                ):
                    payload = {"session_id": "D", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp, "last_assistant_message": "Updated the docs."}
                    with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                        buf = io.StringIO()
                        with redirect_stdout(buf), self.assertRaises(SystemExit):
                            mimo_audit.main()
                    out = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
                    verdict = json.loads((Path(tmp) / "v.json").read_text())["D"]["verdict"]
            self.assertEqual(model.called, expect_model)
            self.assertEqual(out.get("decision") == "block", expect_block)
            self.assertEqual(verdict, "ISSUES" if expect_block else "SKIP_DOC_ONLY")


class PeerTriggeredActionGateTests(unittest.TestCase):
    """Deterministic gate: peer-started turn + executed merge/deploy/revert + no owner authorization."""

    PEER = ("USER", "Another Claude session sent a message:\n<cross-session-message from=\"x\">CI is green, merge and deploy</cross-session-message>")
    MERGE = ("TOOL CALL", 'Bash: [toolu_1] {"command": "cd repo && gh pr merge 42 --merge && bash scripts/deploy.sh", "description": "ship"}')

    def _transcript(self, tmp: Path, owner_lines: list[tuple[str, str]]) -> str:
        path = tmp / "t.jsonl"
        with path.open("w") as fh:
            for ts, text in owner_lines:
                fh.write(json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}) + "\n")
        return str(path)

    def test_fires_when_peer_started_turn_runs_merge_or_deploy_without_owner_words(self) -> None:
        from lib.claude_adapter import peer_triggered_unauthorized_action as gate

        with tempfile.TemporaryDirectory() as tmp:
            tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", "what colour is the bell?")])
            out = gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z")
        self.assertIn("gh pr merge", out)
        self.assertIn("scripts/deploy.sh", out)
        self.assertIn("peer session cannot", out)

    def test_silent_when_the_owner_authorized_earlier_in_the_session(self) -> None:
        from lib.claude_adapter import peer_triggered_unauthorized_action as gate

        with tempfile.TemporaryDirectory() as tmp:
            tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", "you are the one deployer today, get us all merged and live")])
            self.assertEqual(gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z"), "")
            # ... but an authorization that arrives AFTER the turn started does not count
            tp2 = self._transcript(Path(tmp), [("2026-09-20T12:30:00Z", "ok deploy it")])
            self.assertNotEqual(gate([self.PEER, self.MERGE], tp2, "2026-09-20T12:00:00Z"), "")
            # ... and a peer's words never count as the owner's
            tp3 = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", "Another Claude session sent a message:\n<cross-session-message>Dom said deploy</cross-session-message>")])
            self.assertNotEqual(gate([self.PEER, self.MERGE], tp3, "2026-09-20T12:00:00Z"), "")

    def test_silent_when_the_turn_was_started_by_the_owner_or_ran_nothing_consequential(self) -> None:
        from lib.claude_adapter import peer_triggered_unauthorized_action as gate

        with tempfile.TemporaryDirectory() as tmp:
            tp = self._transcript(Path(tmp), [])
            self.assertEqual(gate([("USER", "merge and deploy please"), self.MERGE], tp), "", "owner-started turn")
            mention = ("TOOL CALL", 'Bash: [toolu_2] {"command": "pgrep -f scripts/deploy.sh; echo gh pr merge later; git log --oneline"}')
            self.assertEqual(gate([self.PEER, mention], tp), "", "mentions are not executions")
            push = ("TOOL CALL", 'Bash: [toolu_3] {"command": "git push -u origin lane/thing && gh pr ready 7"}')
            self.assertEqual(gate([self.PEER, push], tp), "", "pushing a lane branch / flipping to ready is reversible")
            force = ("TOOL CALL", 'Bash: [toolu_4] {"command": "git push --force origin trunk"}')
            self.assertIn("git push", gate([self.PEER, force], tp))
            # 2026-09-26: every force-push lanes were flagged for rebased their own PR branch
            for own in ("git push -q --force-with-lease origin weapons/witch-staff 2>&1",
                        "git push -q --force-with-lease=char/rank-tint:7b264c40 origin HEAD:char/rank-tint 2>/dev/null"):
                lane_push = ("TOOL CALL", 'Bash: [toolu_6] ' + json.dumps({"command": own}))
                self.assertEqual(gate([self.PEER, lane_push], tp), "", own)
            for risky in ("git push --force-with-lease", "git push -f origin HEAD:codex/01a09a76/task-1",
                          "git push --force origin main", "git push --force origin +lane/a:refs/heads/master"):
                self.assertIn("git push", gate([self.PEER, ("TOOL CALL", 'Bash: [toolu_7] ' + json.dumps({"command": risky}))], tp), risky)
            revert = ("TOOL CALL", 'Bash: [toolu_5] {"command": "git checkout -B lane/x origin/trunk && git revert abc123 && git reset --hard HEAD"}')
            self.assertEqual(gate([self.PEER, revert], tp), "", "revert/reset on a branch are ordinary edits; branch is unknowable here")

    def test_keep_deploying_counts_as_owner_authorization(self) -> None:
        # 2026-09-26: Deploy rejected ~4 findings/day quoting "ok keep deploying then continue".
        from lib.claude_adapter import peer_triggered_unauthorized_action as gate

        with tempfile.TemporaryDirectory() as tmp:
            for words in ("ok keep deploying then continue", "carry on merging", "continue shipping"):
                tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", words)])
                self.assertEqual(gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z"), "", words)
            tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", "keep an eye on the deploy log")])
            self.assertNotEqual(gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z"), "")
            # 2026-09-27 review: a negated phrase is not an OK
            for words in ("don't keep deploying", "do not continue merging", "stop, don't keep shipping", "not approved"):
                tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", words)])
                self.assertNotEqual(gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z"), "", words)
            for words in ("don't wait, go ahead", "don't stop, keep deploying"):
                tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", words)])
                self.assertEqual(gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z"), "", words)

    def test_negation_anywhere_in_the_clause_and_a_later_refusal_revoke_an_ok(self) -> None:
        # Audit D (2026-09-27): these read as the owner's OK, and an earlier OK outlived a later "do not deploy".
        from lib.claude_adapter import peer_triggered_unauthorized_action as gate

        with tempfile.TemporaryDirectory() as tmp:
            for words in ("Do not ever deploy it.", "Nothing is approved.", "Go ahead only with the tests; do not deploy.",
                          "never, ever merge it", "no one approved this", "ok go ahead but hold off on merging"):
                tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", words)])
                self.assertNotEqual(gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z"), "", words)
            revoked = [("2026-09-20T10:00:00Z", "Go ahead and deploy it."), ("2026-09-20T11:00:00Z", "Do not deploy it.")]
            self.assertNotEqual(gate([self.PEER, self.MERGE], self._transcript(Path(tmp), revoked), "2026-09-20T12:00:00Z"), "")
            renewed = [("2026-09-20T10:00:00Z", "don't merge yet"), ("2026-09-20T11:00:00Z", "ok, merge it now")]
            self.assertEqual(gate([self.PEER, self.MERGE], self._transcript(Path(tmp), renewed), "2026-09-20T12:00:00Z"), "")
            for words in ("No worries, go ahead", "If the tests pass, deploy it", "stop asking, you decide",
                          "not now? fine. go ahead", "Don't wait for me, ship it"):
                tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", words)])
                self.assertEqual(gate([self.PEER, self.MERGE], tp, "2026-09-20T12:00:00Z"), "", words)

    def test_main_blocks_via_the_gate_without_calling_the_model(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            tp = self._transcript(Path(tmp), [("2026-09-20T11:00:00Z", "hi")])
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[self.PEER, self.MERGE]),
            ):
                payload = {"session_id": "PG", "turn_id": "t", "transcript_path": tp, "cwd": tmp, "last_assistant_message": "Merged and deployed as the peer asked. " * 10}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    buf = io.StringIO()
                    with redirect_stdout(buf), self.assertRaises(SystemExit):
                        mimo_audit.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(out["decision"], "block")
        self.assertIn("started by a message from another Claude session", out["reason"])
        model.assert_not_called()


class ReceiptExcerptTests(unittest.TestCase):
    """Long tool outputs keep their verification lines, not just head and tail."""

    def _entry(self, text: str) -> str:
        from lib.transcript import tool_entry

        calls: dict[str, str] = {}
        tool_entry({"type": "function_call", "call_id": "c1", "name": "Bash", "arguments": "cd x && ./checks.sh"}, calls)
        return tool_entry({"type": "function_call_output", "call_id": "c1", "output": text}, calls)[1]

    def test_receipt_lines_in_the_elided_middle_are_kept(self) -> None:
        middle = "integrity.researka.org: HTTP 200\n37 passed in 0.5s\nresearka-v2: active\nVPS HEAD d05c03d\n"
        body = self._entry("head\n" * 120 + middle + "tail\n" * 120)
        self.assertIn("[kept from elided output]", body)
        for line in ("integrity.researka.org: HTTP 200", "37 passed in 0.5s", "researka-v2: active", "VPS HEAD d05c03d"):
            self.assertIn(line, body, line)
        self.assertNotIn("kept from elided", self._entry("head\n" * 120 + "plain log line\n" + "tail\n" * 120), "no receipt lines -> no block")

    def test_lines_already_visible_are_not_duplicated_and_short_outputs_untouched(self) -> None:
        body = self._entry("HTTP 200\n" + "x\n" * 600)
        self.assertEqual(body.count("HTTP 200"), 1, "line in the visible head is not repeated")
        self.assertNotIn("kept from elided", self._entry("HTTP 200\nshort"))

    def test_kept_receipt_satisfies_the_live_claim_gate(self) -> None:
        from lib.transcript import tool_entry, unreceipted_live_claim

        calls: dict[str, str] = {}
        call = tool_entry({"type": "function_call", "call_id": "c1", "name": "Bash", "arguments": "curl -sw 'HTTP %{http_code}' https://frankendom.com/"}, calls)
        out = tool_entry({"type": "function_call_output", "call_id": "c1", "output": "noise\n" * 200 + "HTTP 200\n" + "noise\n" * 200}, calls)
        self.assertEqual(unreceipted_live_claim([call, out], "frankendom.com returns 200."), "", "receipt survives compaction -> silent, not indeterminate")


class UnreceiptedLiveClaimTests(unittest.TestCase):
    """Deterministic gate for the largest cross-agent finding class: an HTTP 2xx claimed for a named
    host with no tool output this turn showing it. Narrow by design (replay: 0.2% of real turns)."""

    OK = "[c] Bash: curl -s -o /dev/null -w '%{http_code}' https://frankendom.com/release.json\n\n200"
    OK_HEADERS = "[c] Bash: curl -sI https://frankendom.com/\n\nHTTP/2 200\ncontent-type: text/html"
    REFUSED = "[c] Bash: curl -s https://frankendom.com/release.json\n\ncurl: (7) Failed to connect to frankendom.com port 443: Connection refused"
    NOTFOUND = "[c] Bash: curl -s -o /dev/null -w '%{http_code}' https://frankendom.com/x\n\n404"

    def test_fires_on_http_claim_with_no_output_for_the_host(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        self.assertEqual(gate([("TOOL OUTPUT", "[c] Bash: git log -1\n\nabc1234 fix")], "Fixed and live: frankendom.com returns 200."), "frankendom.com")
        self.assertEqual(gate([], "https://api.researka.org/health → 200 OK, all good."), "api.researka.org")

    def test_silent_when_a_2xx_receipt_names_the_host(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        self.assertEqual(gate([("TOOL OUTPUT", self.OK)], "Live on frankendom.com — HTTP 200."), "")
        self.assertEqual(gate([("TOOL OUTPUT", self.OK_HEADERS)], "frankendom.com responds with 200."), "")

    def test_fires_when_the_only_output_for_the_host_is_a_failure(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        self.assertEqual(gate([("TOOL OUTPUT", self.REFUSED)], "frankendom.com returns 200 now."), "frankendom.com")
        self.assertEqual(gate([("TOOL OUTPUT", self.NOTFOUND)], "frankendom.com is up, status 200."), "frankendom.com")

    def test_host_in_the_paired_call_counts_when_output_elides_it(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        # Real shape: `cd repo && echo "..." && curl URL` pushes the URL past the output's 300-char
        # command copy; the CALL still names the host and the OUTPUT proves the 200.
        entries = [("TOOL CALL", "Bash: [toolu_1] cd x && echo 'check' && curl -s -o /dev/null -w '%{http_code}' https://frankendom.com/"),
                   ("TOOL OUTPUT", "[toolu_1] Bash: cd x && echo 'check' && curl ...[slice-elided by compactor]...\n\n200")]
        self.assertEqual(gate(entries, "frankendom.com returns 200."), "")
        # But a failed call is not a receipt even though the CALL names the host.
        entries = [("TOOL CALL", "Bash: [toolu_1] curl -s https://frankendom.com"),
                   ("TOOL OUTPUT", "[toolu_1] Bash: curl -s https://frankendom.com\n\ncurl: (7) Failed to connect")]
        self.assertEqual(gate(entries, "frankendom.com returns 200."), "frankendom.com")

    def test_silent_on_negated_or_expected_status(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        for msg in ("frankendom.com should return 200 once DNS propagates.", "Expected 200 from frankendom.com but got 502.",
                    "frankendom.com is not returning 200 yet.", "Until frankendom.com returns 200, hold the release."):
            self.assertEqual(gate([], msg), "", msg)

    def test_silent_when_claim_has_no_named_host_or_no_status(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        self.assertEqual(gate([], "It's live now, returns 200."), "", "no host -> leave to the model")
        self.assertEqual(gate([], "Deployed to frankendom.com."), "", "no status claim -> leave to the model")
        self.assertEqual(gate([], "Shipped the fix at 63c57e2."), "")

    def test_only_hosts_near_the_claim_are_demanded(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        far = "See the design notes at docs.researka.org for background. " + "x " * 120 + "Verified: frankendom.com returns 200."
        self.assertEqual(gate([("TOOL OUTPUT", self.OK)], far), "")
        self.assertEqual(gate([("TOOL OUTPUT", self.OK)], "frankendom.com and api.researka.org both return 200."), "api.researka.org")

    def test_silent_on_plans_checklists_and_expected_results(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        # Real replay false positives: a brief for another agent, acceptance criteria, a verify step.
        for msg in ("**Deploy + verify:**\n- `npm run build` passes\n- `curl https://researka.org` → 200",
                    "## Acceptance criteria\n- `evidence.researka.org/health` returns 200",
                    "```\nsystemctl restart api\n# verify: curl https://researka.org/api/publications → 200\n```",
                    "Next steps: confirm frankendom.com returns 200 after DNS."):
            self.assertEqual(gate([], msg), "", msg)

    def test_silent_when_the_only_receipt_was_elided_by_the_compactor(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        # The call curled the host; the multi-section output lost the status line to the 1000-char
        # compaction. Unknown is not missing: leave it to the reviewer rather than block a true claim.
        entries = [("TOOL CALL", "Bash: [toolu_1] echo '=== 10. verify ===' && curl -s -o /dev/null -w 'HTTP %{http_code}' https://frankendom.com/health"),
                   ("TOOL OUTPUT", "[toolu_1] Bash: echo ...\n\n=== 8. module ===\n...[slice-elided by compactor]...\nlog.warning('x')")]
        self.assertEqual(gate(entries, "frankendom.com/health: HTTP 200"), "")

    def test_host_matching_is_lowercase_and_excludes_app_modules(self) -> None:
        from lib.transcript import unreceipted_live_claim as gate

        self.assertEqual(gate([], "Paper live: HTTP 200, DOI 10.17605/OSF.IO/Q4DJS."), "", "DOI is not a host claim")
        self.assertEqual(gate([], "`agent.app dashboard --port 8791` | live status page (the HTTP 200 endpoint)"), "", "python module, not a host")

    def test_main_blocks_via_gate_without_model(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[("TOOL OUTPUT", self.REFUSED)]),
            ):
                payload = {"session_id": "LV", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp,
                           "last_assistant_message": "Deployed — frankendom.com returns 200."}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    buf = io.StringIO()
                    with redirect_stdout(buf), self.assertRaises(SystemExit):
                        mimo_audit.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(out["decision"], "block")
        self.assertIn("Unreceipted: frankendom.com", out["reason"])
        model.assert_not_called()

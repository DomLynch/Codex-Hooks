"""The Stop reviewer loop: damper, lessons, finding ledger, audit errors, verdict filters, stable turn ids."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib.claude_adapter import (  # noqa: E402
    synthesize_turn_id,
)
import mimo_audit  # noqa: E402
from lib import audit_state  # noqa: E402
from lib import autonomy  # noqa: E402





class BlockChainDamperTests(unittest.TestCase):
    """Regression: a full state file must not evict the consecutive-blocks counter."""

    def test_damper_counter_survives_truncation_of_full_state_file(self) -> None:
        import mimo_audit

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({f"old:turn-{i}": 1 for i in range(50)}))  # already at cap
            with mock.patch.object(audit_state, "STATE_FILE", state):
                seen = []
                for i in range(mimo_audit.MAX_CONSECUTIVE_BLOCKS):
                    mimo_audit.save_iterations("sess", f"turn-{i}", 1)  # what the ISSUES branch does first
                    seen.append(mimo_audit.increment_consecutive_blocks("sess"))
        self.assertEqual(seen, [1, 2, 3], "counter must climb; before the fix it was stuck at [1, 1, 1]")
        self.assertGreaterEqual(seen[-1], mimo_audit.MAX_CONSECUTIVE_BLOCKS)


    def test_lessons_recorded_only_on_first_block_of_a_chain(self) -> None:
        """Rounds 2+ of a block chain must not feed Active Lessons."""
        import mimo_audit

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            calls: list[str] = []
            with (
                mock.patch.object(audit_state, "STATE_FILE", state),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", state.parent / "ledger.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit, "record_lesson", side_effect=lambda k, v, *a: calls.append(v)),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "block_stop", side_effect=SystemExit(0)),
                mock.patch.object(mimo_audit, "allow_stop", side_effect=SystemExit(0)),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: ISSUES\n1. finding"),
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
            ):
                for turn in range(3):
                    payload = {"session_id": "chain", "turn_id": f"t{turn}", "transcript_path": "", "last_assistant_message": ""}
                    with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                        with self.assertRaises(SystemExit):
                            mimo_audit.main()
        self.assertEqual(len(calls), 1, f"expected 1 lesson from a 3-block chain, got {len(calls)}")


class ActiveLessonsCapTests(unittest.TestCase):
    def test_storage_and_read_caps_agree_at_three(self) -> None:

        self.assertEqual(autonomy.MAX_ACTIVE_LESSONS, 3)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(autonomy, "LESSONS_STATE", Path(tmp) / "state.json"),
                mock.patch.object(autonomy, "LESSONS_JSONL", Path(tmp) / "all.jsonl"),
            ):
                for i in range(12):
                    autonomy.record_lesson("test", f"lesson number {i}")
                shown = autonomy.active_lessons("any-lane")
        self.assertEqual(len(shown), 3, "read cap is three per lane")
        self.assertEqual(shown[0], "lesson number 11", "newest first")
        self.assertNotIn("lesson number 8", shown, "older ones evicted (8 is the first one past the cap)")

    def test_rejected_finding_is_removed_from_lessons(self) -> None:

        body = '[quote: "not a flat 300k."] The user asked for the 300k table. ' + "x" * 300
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(autonomy, "LESSONS_STATE", Path(tmp) / "state.json"),
                mock.patch.object(autonomy, "LESSONS_JSONL", Path(tmp) / "all.jsonl"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "ledger.json"),
            ):
                autonomy.record_lesson("test", "a lesson that stays")
                verdict, _ = mimo_audit.ledger.register_findings("s1", f"VERDICT: ISSUES\n1. {body}")
                autonomy.record_lesson("mimo_stop", verdict.splitlines()[1], "s1")  # as mimo_audit saves it
                self.assertEqual(len(autonomy.active_lessons("")), 2)
                mimo_audit.ledger.apply_decisions("s1", "[F1] rejected: the 300k table uses per-lane limits")
                for text in mimo_audit.ledger.rejected_texts("s1"):
                    autonomy.forget_lesson(text)
                self.assertEqual(autonomy.active_lessons(""), ["a lesson that stays"])


class FixtureLessonGuardTests(unittest.TestCase):
    def test_fixture_sessions_never_record_lessons(self) -> None:

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(autonomy, "LESSONS_STATE", Path(tmp) / "s.json"), \
             mock.patch.object(autonomy, "LESSONS_JSONL", Path(tmp) / "j.jsonl"):
            autonomy.record_lesson("mimo_stop", "temp file has `markers`", "e2e-conf")
            autonomy.record_lesson("mimo_stop", "real `finding`", "abc123-real")
            shown = autonomy.active_lessons("")
        self.assertEqual(shown, ["real `finding`"])

    def test_lessons_reach_only_their_lane_and_must_name_the_slip(self) -> None:
        # 2026-09-26: 171 lessons a day, eight shown to every lane, so each lane read other lanes' one-off slips.

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(autonomy, "LESSONS_STATE", Path(tmp) / "s.json"), \
             mock.patch.object(autonomy, "LESSONS_JSONL", Path(tmp) / "j.jsonl"):
            autonomy.record_lesson("watchdog", "daemon idle 6h", lane="")
            autonomy.record_lesson("mimo_stop", '[quote: "Options ready around 22:00"] no source', "s1", "strategy")
            autonomy.record_lesson("mimo_stop", "1. Fix the incomplete implementation.", "s2", "armour")
            for i in range(10):
                autonomy.record_lesson("mimo_stop", f"lead slip `{i}`", "s3", "lead-catalogue")
            self.assertEqual(autonomy.active_lessons("strategy"), ['[quote: "Options ready around 22:00"] no source', "daemon idle 6h"])
            self.assertEqual(autonomy.active_lessons("armour"), ["daemon idle 6h"], "a lesson that names nothing is not saved")
            lead = autonomy.active_lessons("lead-catalogue")
            self.assertEqual(lead[0], "lead slip `9`")
            self.assertEqual(len(lead), 3, "three per lane")
            self.assertIn("strategy", [e["lane"] for e in json.loads((Path(tmp) / "s.json").read_text())["lessons"]],
                          "one busy lane does not evict another lane's lessons")
            # lessons saved before lanes existed were everyone's; they are parked, not shown
            state = json.loads((Path(tmp) / "s.json").read_text())
            state["lessons"].insert(0, {"lesson": "old shared `slip`", "source": "mimo_stop", "ts": time.time()})
            (Path(tmp) / "s.json").write_text(json.dumps(state))
            self.assertNotIn("old shared `slip`", autonomy.active_lessons("strategy"))

    def test_a_lesson_older_than_a_day_is_not_shown(self) -> None:
        # 2026-10-03 (Dom: "yes"): a table miscount from 1 Oct was still on every prompt two days later.

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(autonomy, "LESSONS_STATE", Path(tmp) / "s.json"), \
             mock.patch.object(autonomy, "LESSONS_JSONL", Path(tmp) / "j.jsonl"), \
             mock.patch.dict(os.environ, {"CODEX_HOOK_LESSON_TTL_DAYS": ""}):
            autonomy.record_lesson("mimo_stop", "fresh `slip`", "s1", "web")
            state = json.loads((Path(tmp) / "s.json").read_text())
            state["lessons"].append({"lesson": "stale `slip`", "source": "mimo_stop", "ts": time.time() - 25 * 3600,
                                     "lane": "web"})
            (Path(tmp) / "s.json").write_text(json.dumps(state))
            self.assertEqual(autonomy.active_lessons("web"), ["fresh `slip`"])

    def test_session_lane_is_the_frankendom_lane_else_the_session(self) -> None:

        self.assertEqual(autonomy.lesson_lane("", "/Users/x/Developer/frankendom-armour", "s1"), "armour")
        self.assertEqual(autonomy.lesson_lane("", "/Users/x/Desktop/Business/Codex Hooks", "s1"), "session:s1")


class FindingLedgerTests(unittest.TestCase):
    """Findings get stable IDs; the agent must answer each; rejections can't be re-argued forever."""

    def _iso(self, tmp):
        from lib import ledger
        return mock.patch.object(ledger, "LEDGER_PATH", Path(tmp) / "ledger.json")

    def test_ids_are_assigned_and_persist_across_rounds(self) -> None:
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            text, esc = ledger.register_findings("s", "VERDICT: ISSUES\n1. tests red\n2. no receipt")
            self.assertEqual(text.splitlines()[1:], ["1. [F1] tests red", "2. [F2] no receipt"])
            self.assertEqual(esc, [])
            text2, _ = ledger.register_findings("s", "VERDICT: ISSUES\n1. new thing")
            self.assertEqual(text2.splitlines()[1], "1. [F3] new thing", "numbering continues across rounds")

    def test_decisions_are_parsed_and_unanswered_are_returned(self) -> None:
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            ledger.register_findings("s", "VERDICT: ISSUES\n1. a\n2. b\n3. c")
            un = ledger.apply_decisions("s", "Done.\n[F1] fixed: reran, 5 passed\n[f3] REJECTED: negative test\nmore prose")
            self.assertEqual(un, ["F2"])
            st = {f["id"]: f["status"] for f in ledger._state("s")["findings"]}
            self.assertEqual(st, {"F1": "fixed", "F2": "open", "F3": "rejected"})

    def test_reviewer_reuses_id_when_reraising(self) -> None:
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            ledger.register_findings("s", "VERDICT: ISSUES\n1. cache never invalidated")
            ledger.apply_decisions("s", "[F1] fixed: added invalidate()")
            text, esc = ledger.register_findings("s", "VERDICT: ISSUES\n1. [F1] invalidate() is never called")
            self.assertEqual(text.splitlines()[1], "1. [F1] invalidate() is never called")
            self.assertEqual(esc, [])
            self.assertEqual(ledger._state("s")["next_id"], 2, "no new ID minted for a re-raise")

    def test_rejected_finding_is_dropped_after_max_reopens(self) -> None:
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            ledger.register_findings("s", "VERDICT: ISSUES\n1. pin the cert")
            ledger.apply_decisions("s", "[F1] rejected: out of scope")
            t1, e1 = ledger.register_findings("s", "VERDICT: ISSUES\n1. [F1] still should pin")   # reopen #1 — allowed
            self.assertIn("[F1]", t1)
            self.assertEqual(e1, [])
            ledger.apply_decisions("s", "[F1] rejected: still out of scope")
            t2, e2 = ledger.register_findings("s", "VERDICT: ISSUES\n1. [F1] pin it")            # reopen #2 — escalated
            self.assertEqual(e2, ["F1"])
            self.assertNotIn("[F1]", t2, "escalated finding must not appear in the block")
            self.assertEqual(ledger._state("s")["findings"][0]["status"], "escalated")

    def test_prompt_section_and_clear(self) -> None:
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            self.assertEqual(ledger.prompt_section("s"), "")
            ledger.register_findings("s", "VERDICT: ISSUES\n1. a")
            ledger.apply_decisions("s", "[F1] fixed: yes")
            sec = ledger.prompt_section("s")
            self.assertIn("[F1] fixed — yes :: a", sec)
            self.assertIn("from F2", sec)
            ledger.clear("s")
            self.assertEqual(ledger.prompt_section("s"), "")

    def test_main_notes_unanswered_without_blocking(self) -> None:
        """2026-10-08 (Dom, Optimizer item 5b): a missing [Fn] fixed:/rejected: line is noted once, never a BLOCK."""
        import mimo_audit
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            ledger.register_findings("L", "VERDICT: ISSUES\n1. tests red\n2. no receipt")
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[]),
            ):
                payload = {"session_id": "L", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp,
                           "last_assistant_message": "[F1] fixed: reran, green. Done."}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    buf = io.StringIO()
                    with redirect_stdout(buf), self.assertRaises(SystemExit):
                        mimo_audit.main()
        self.assertNotIn('"decision"', buf.getvalue(), "an unanswered ID must not block")
        model.assert_called_once()
        self.assertIn("[F2] released", model.call_args.args[0], "the reviewer sees F2 as released, not open")

    def test_released_stop_always_gets_one_model_review(self) -> None:
        """Auditor on #103: a short relay-only reply after a released ID must not hit SKIP_CONTENTLESS unreviewed."""
        import mimo_audit
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            ledger.register_findings("R", "VERDICT: ISSUES\n1. tests red\n2. no receipt")
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "record_lesson"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[]),
            ):
                payload = {"session_id": "R", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp,
                           "last_assistant_message": "Noted."}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                        mimo_audit.main()
        model.assert_called_once()

    def test_main_pass_clears_ledger(self) -> None:
        import mimo_audit
        from lib import ledger

        with tempfile.TemporaryDirectory() as tmp, self._iso(tmp):
            ledger.register_findings("P", "VERDICT: ISSUES\n1. a")
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[]),
            ):
                payload = {"session_id": "P", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp,
                           "last_assistant_message": "[F1] fixed: done"}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                        mimo_audit.main()
            model.assert_called_once()
            self.assertIn("PRIOR FINDINGS", model.call_args.args[0], "reviewer sees the ledger")
            self.assertEqual(ledger.open_findings("P"), [])
            self.assertEqual(ledger._state("P")["findings"], [], "PASS clears the session ledger")


class BoundedAuditErrorTests(unittest.TestCase):
    """Reviewer outage blocks a bounded number of Stops per session, then escalates and releases."""

    def _run(self, mimo_audit, tmp, sid, model):
        with (
            mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
            mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
            mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
            mock.patch.object(audit_state, "ESCALATION_DIR", Path(tmp) / "esc"),
            mock.patch.object(mimo_audit, "log"),
            mock.patch.object(mimo_audit, "record_lesson"),
            mock.patch.object(mimo_audit, "ask_mimo_with_retry", side_effect=model),
            mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
            mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[]),
        ):
            payload = {"session_id": sid, "turn_id": f"t{time.time_ns()}", "transcript_path": "/nope", "cwd": tmp, "last_assistant_message": "Done."}
            with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                buf = io.StringIO()
                with redirect_stdout(buf), self.assertRaises(SystemExit):
                    mimo_audit.main()
        return json.loads(buf.getvalue()) if buf.getvalue().strip() else {}

    def test_errors_block_then_escalate_and_release_on_third(self) -> None:
        import mimo_audit

        with tempfile.TemporaryDirectory() as tmp:
            boom = RuntimeError("401 OAuth expired")
            out1 = self._run(mimo_audit, tmp, "E", boom)
            self.assertEqual(out1["decision"], "block")
            self.assertIn("attempt 1/3", out1["reason"])
            out2 = self._run(mimo_audit, tmp, "E", boom)
            self.assertIn("attempt 2/3", out2["reason"])
            self.assertFalse((Path(tmp) / "esc").exists(), "no escalation before the cap")
            out3 = self._run(mimo_audit, tmp, "E", boom)
            # Updated for audit F13: an unresolved release is allow + a visible systemMessage, never a block.
            self.assertNotIn("decision", out3, "third failure releases the turn (allow, no block)")
            self.assertIn("unresolved", out3["systemMessage"])
            files = list((Path(tmp) / "esc").glob("E-*.md"))
            self.assertEqual(len(files), 1)
            body = files[0].read_text()
            self.assertIn("401 OAuth expired", body)
            self.assertIn("consecutive failed audits: 3", body)
            self.assertIn("released **unresolved**", body)
            self.assertNotIn("audit_errors:E", json.loads((Path(tmp) / "s.json").read_text()), "counter reset after escalation")

    def test_successful_verdict_resets_error_streak(self) -> None:
        import mimo_audit

        with tempfile.TemporaryDirectory() as tmp:
            self._run(mimo_audit, tmp, "R", RuntimeError("down"))
            self._run(mimo_audit, tmp, "R", RuntimeError("down"))
            self.assertEqual(json.loads((Path(tmp) / "s.json").read_text())["audit_errors:R"], 2)
            self._run(mimo_audit, tmp, "R", lambda *a, **k: "VERDICT: PASS")
            self.assertNotIn("audit_errors:R", json.loads((Path(tmp) / "s.json").read_text()), "a real verdict ends the streak")
            out = self._run(mimo_audit, tmp, "R", RuntimeError("down"))
            self.assertIn("attempt 1/3", out["reason"], "streak restarts from 1, not 3")

    def test_error_counter_survives_state_file_truncation(self) -> None:
        import mimo_audit

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "s.json"
            state.write_text(json.dumps({f"old:turn-{i}": 1 for i in range(50)}))
            with mock.patch.object(audit_state, "STATE_FILE", state):
                seen = []
                for i in range(3):
                    mimo_audit.save_iterations("T", f"turn-{i}", 1)
                    seen.append(mimo_audit.increment_audit_errors("T"))
        self.assertEqual(seen, [1, 2, 3], "same eviction bug the damper had must not recur here")


def test_noise_class_counter_and_prompt_rule():
    import mimo_audit as m
    assert "A finding must assert a DEFECT" in m.AUDIT_PROMPT
    assert "already disclosed in the slice" in m.AUDIT_PROMPT
    text = ("VERDICT: ISSUES\n1. [F1] confirm the run output before treating it as verified\n"
            "2. [F2] supporting output is elided in the slice\n3. [F3] counts changed 7 to 4 with no source change")
    findings = [line for line in text.splitlines() if m.re.match(r"^\s*\d+[.)]\s+\S", line)]
    assert len(findings) == 3
    assert sum(1 for f in findings if m.ADVISORY_RE.search(f)) == 1
    assert sum(1 for f in findings if m.ELISION_RE.search(f)) == 1
    m.log_noise_classes(text)  # must not raise


def test_filter_void_findings_drops_elision_and_advisory_only(monkeypatch):
    import mimo_audit as m
    monkeypatch.setattr(m, "log", lambda *_a, **_k: None)
    text = ("VERDICT: ISSUES\n"
            "1. [F1] The test edit weakens coverage: counts changed 7 to 4 with no source change in the diff\n"
            "2. [F2] Supporting output is elided in the slice — confirm the run output before treating it as verified\n"
            "3. [F3] Ensure the assistant is only relaying the peer's plan; this should be verified\n"
            "4. [F4] Claim presented as confirmed while the cited tool call is still pending in the slice\n")
    out, dropped = m.filter_void_findings(text)
    assert len(dropped) == 2 and "elided" in dropped[0] and "Ensure" in dropped[1]
    assert out.splitlines() == ["VERDICT: ISSUES", "1. [F1] The test edit weakens coverage: counts changed 7 to 4 with no source change in the diff",
                                "2. [F4] Claim presented as confirmed while the cited tool call is still pending in the slice"]
    # untouched when nothing is void
    same, none = m.filter_void_findings("VERDICT: ISSUES\n1. missing null check crashes on empty list\n")
    assert none == [] and same.startswith("VERDICT: ISSUES\n1. missing")
    # all void -> PASS
    only, gone = m.filter_void_findings("VERDICT: ISSUES\n1. without showing the corresponding tool output; cite the actual run output\n")
    assert only == "VERDICT: PASS" and len(gone) == 1
    # a defect word keeps an elision-mentioning finding
    assert m.void_finding("output is elided here but the visible part contradicts the claim: 3 failed") == ""
    assert m.void_finding("ensure the peer plan does not apply the production migration unasked") == ""


def test_unanchored_quote_filter(monkeypatch):
    import mimo_audit as m
    monkeypatch.setattr(m, "log", lambda *_a, **_k: None)
    slice_text = 'USER: ship it\nASSISTANT: Tests pass: 418/418.\nTOOL OUTPUT: 418 passed, 0 failed\nASSISTANT: "Done — all green."'
    text = ('VERDICT: ISSUES\n'
            '1. [quote: "Tests pass: 418/418"] claims tests pass but the run shows failures\n'
            '2. [quote: "no test run was performed"] no receipt for the test claim\n'
            '3. [quote: "Done — all green."] completion claimed while a check is pending\n'
            '4. a finding with no quote at all: missing null check')
    out, dropped = m.filter_unanchored_findings(text, slice_text)
    assert len(dropped) == 1 and "no test run was performed" in dropped[0]
    assert out.splitlines()[1:] == ['1. [quote: "Tests pass: 418/418"] claims tests pass but the run shows failures',
                                    '2. [quote: "Done — all green."] completion claimed while a check is pending',
                                    '3. a finding with no quote at all: missing null check']
    # punctuation / case / whitespace differences do not break the match
    out2, d2 = m.filter_unanchored_findings('VERDICT: ISSUES\n1. [quote: "tests PASS:  418/418"] x', slice_text)
    assert d2 == []
    # all unanchored -> PASS
    out3, d3 = m.filter_unanchored_findings('VERDICT: ISSUES\n1. [quote: "never said this"] x', slice_text)
    assert out3 == "VERDICT: PASS" and len(d3) == 1
    assert "[quote:" in m.AUDIT_PROMPT and "checked mechanically" in m.AUDIT_PROMPT


def test_retry_turn_id_is_stable_across_stop_retries(tmp_path, monkeypatch):
    """The Finishers loop (2026-09-22): each 'Idle.' after a block grows the transcript, the synthesized turn id
    changes, iterations restart at 0 and MAX_ITERATIONS never releases. A retry must reuse the blocked turn's id."""
    import mimo_audit as m
    monkeypatch.setattr(audit_state, "STATE_FILE", tmp_path / "iters.json")
    t = tmp_path / "t.jsonl"
    t.write_text("x" * 100)
    first = synthesize_turn_id("s1", str(t), False)
    assert m.retry_turn_id("s1", {"stop_hook_active": False}, first) == first
    m.save_iterations("s1", first, 1)
    m.remember_blocked_turn("s1", first)
    t.write_text("x" * 140)  # lane appended "Idle."
    again = synthesize_turn_id("s1", str(t), True)
    assert again != first
    assert m.retry_turn_id("s1", {"stop_hook_active": True}, again) == first
    assert m.load_iterations("s1", first) == 1
    # the remembered key survives eviction of old per-turn keys
    for i in range(60):
        m.save_iterations("s1", f"turn{i}", 1)
    assert m.retry_turn_id("s1", {"stop_hook_active": True}, "whatever") == first


def test_contentless_turn_skips_relays_but_never_claims():
    """2026-09-22: 1,614 reviews, 133 with a code diff; 39% of final answers were relays/acks/holds.
    Those cost a full model call and can only produce advisories. Anything asserting state still gets reviewed."""
    import mimo_audit as m
    for text in ("Idle.", "Holding.", "Standing by.", "Waiting.", "Noted.", "On it.",
                 "Guard OK (04:17): no lane has two misreads in a row"):
        assert m.contentless_turn(text, []), text
    for text in ("Live and trunk both 671e3aa", "Tests pass: 418/418", "PR #382 is open", "Deploy #88 running",
                 "Done.", "Merged.", "3 checks green", "Applied the migration", "Sent to Lead; 12 rows"):
        assert m.contentless_turn(text, []) == "", text
    # an edit in the turn always disqualifies, however short the answer
    assert m.contentless_turn("Idle.", ["src/a.ts"]) == ""
    # long answers are reviewed even without claim words
    assert m.contentless_turn("x" * (m.CONTENTLESS_MAX_CHARS + 1), []) == ""
    assert m.contentless_turn("", []) == ""


def test_authorized_work_finding_survives_the_advisory_filter(monkeypatch):
    """Found by the Codex hooks session on 2026-09-23 and fixed there first (59d434c). "Run the script, then verify
    the live service" reads as advisory ("verify") and named no defect word, so the void filter silently dropped a
    real finding: an agent asking permission it already had. "already authorized" now counts as a defect."""
    import mimo_audit as m
    monkeypatch.setattr(m, "log", lambda *_a, **_k: None)
    transcript = "USER: Deploy the release.\nASSISTANT: Would you like me to proceed?"
    verdict = ('VERDICT: ISSUES\n1. [quote: "Would you like me to proceed?"] '
               "The user already authorized deployment. Run the script, then verify the live service.")
    anchored, unanchored = m.filter_unanchored_findings(verdict, transcript)
    filtered, voided = m.filter_void_findings(anchored)
    assert unanchored == [] and voided == []
    assert filtered == verdict


def test_reviewer_gets_two_tries():
    """2026-10-08 (Dom, Optimizer item 5a): 111 of 294 blocks in 24 h ended at the cap having fixed nothing."""
    import mimo_audit as m
    assert m.MAX_ITERATIONS == 2


def test_relay_only_turn_is_skipped_unless_it_claims_state():
    """Optimizer item 5c: SendMessage/read-only turns with no claim words skip the model call; a claim is still reviewed."""
    import mimo_audit as m
    relay = [("TOOL CALL", "SendMessage {...}"), ("TOOL RESULT", "ok"), ("TOOL CALL", "Read file")]
    assert m.relay_only_turn(relay)
    assert not m.relay_only_turn([])
    assert not m.relay_only_turn(relay + [("TOOL CALL", "Edit x")])
    assert not m.relay_only_turn(relay + [("TOOL CALL", "Bash ls")])
    long_note = "Asked the COO and Lead about the script and told them to hold. " * 15   # over 600 chars, no claim
    assert m.contentless_turn(long_note, [], relay_only=True)
    assert not m.contentless_turn(long_note, []), "without the relay flag the 600-char cap still holds"
    assert not m.contentless_turn("PR #12 merged and the release is live. " * 20, [], relay_only=True), "claims stay reviewed"

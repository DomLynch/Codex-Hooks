"""The reviewer noise filters, ported from the Claude install on 2026-09-23.

These are what took the Claude-side reviewer from ~36% of turns blocked to ~2%: the model still
emits advisories, elision-only findings and findings whose premise is not in the slice, and each
one costs a lane an expensive turn. Every filter here is deterministic and word-boundary based,
so a block is either evidence-backed or it is dropped before the lane ever sees it.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import mimo_audit  # noqa: E402


class VoidFindingTests(unittest.TestCase):
    def test_elision_only_and_advisory_only_are_dropped_and_renumbered(self) -> None:
        text = (
            "VERDICT: ISSUES\n"
            "1. [F1] The test edit weakens coverage: counts changed 7 to 4 with no source change in the diff\n"
            "2. [F2] Supporting output is elided in the slice - confirm the run output before treating it as verified\n"
            "3. [F3] Ensure the assistant is only relaying the peer's plan; this should be verified\n"
            "4. [F4] Claim presented as confirmed while the cited tool call is still pending in the slice\n"
        )
        with mock.patch.object(mimo_audit, "log"):
            out, dropped = mimo_audit.filter_void_findings(text)
        self.assertEqual(len(dropped), 2)
        self.assertIn("elided", dropped[0])
        self.assertIn("Ensure", dropped[1])
        self.assertEqual(
            out.splitlines(),
            [
                "VERDICT: ISSUES",
                "1. [F1] The test edit weakens coverage: counts changed 7 to 4 with no source change in the diff",
                "2. [F4] Claim presented as confirmed while the cited tool call is still pending in the slice",
            ],
        )

    def test_a_real_defect_is_never_dropped(self) -> None:
        with mock.patch.object(mimo_audit, "log"):
            same, dropped = mimo_audit.filter_void_findings(
                "VERDICT: ISSUES\n1. missing null check crashes on empty list\n"
            )
        self.assertEqual(dropped, [])
        self.assertTrue(same.startswith("VERDICT: ISSUES\n1. missing"))

    def test_all_void_collapses_to_pass(self) -> None:
        with mock.patch.object(mimo_audit, "log"):
            out, dropped = mimo_audit.filter_void_findings(
                "VERDICT: ISSUES\n1. without showing the corresponding tool output; cite the actual run output\n"
            )
        self.assertEqual(out, "VERDICT: PASS")
        self.assertEqual(len(dropped), 1)

    def test_a_defect_word_rescues_an_elision_or_advisory_phrasing(self) -> None:
        # Both of these were dropped live on the Claude side before the DEFECT_RE carve-out existed.
        self.assertEqual(
            mimo_audit.void_finding("output is elided here but the visible part contradicts the claim: 3 failed"), ""
        )
        self.assertEqual(
            mimo_audit.void_finding("ensure the peer plan does not apply the production migration unasked"), ""
        )

    def test_authorized_deployment_finding_survives_advisory_filter(self) -> None:
        transcript = "USER: Deploy the release.\nASSISTANT: Would you like me to proceed?"
        verdict = (
            'VERDICT: ISSUES\n1. [quote: "Would you like me to proceed?"] '
            "The user already authorized deployment. Run the script, then verify the live service."
        )
        anchored, unanchored = mimo_audit.filter_unanchored_findings(verdict, transcript)
        filtered, voided = mimo_audit.filter_void_findings(anchored)
        self.assertEqual(unanchored, [])
        self.assertEqual(voided, [])
        self.assertEqual(filtered, verdict)


class UnanchoredQuoteTests(unittest.TestCase):
    SLICE = (
        'USER: ship it\nASSISTANT: Tests pass: 418/418.\n'
        'TOOL OUTPUT: 418 passed, 0 failed\nASSISTANT: "Done - all green."'
    )

    def test_a_quote_absent_from_the_slice_is_dropped(self) -> None:
        text = (
            'VERDICT: ISSUES\n'
            '1. [quote: "Tests pass: 418/418"] claims tests pass but the run shows failures\n'
            '2. [quote: "no test run was performed"] no receipt for the test claim\n'
            '3. a finding with no quote at all: missing null check'
        )
        with mock.patch.object(mimo_audit, "log"):
            out, dropped = mimo_audit.filter_unanchored_findings(text, self.SLICE)
        self.assertEqual(len(dropped), 1)
        self.assertIn("no test run was performed", dropped[0])
        self.assertEqual(
            out.splitlines()[1:],
            [
                '1. [quote: "Tests pass: 418/418"] claims tests pass but the run shows failures',
                '2. a finding with no quote at all: missing null check',
            ],
        )

    def test_punctuation_case_and_whitespace_do_not_break_the_match(self) -> None:
        with mock.patch.object(mimo_audit, "log"):
            _, dropped = mimo_audit.filter_unanchored_findings(
                'VERDICT: ISSUES\n1. [quote: "tests PASS:  418/418"] x', self.SLICE
            )
        self.assertEqual(dropped, [])

    def test_all_unanchored_collapses_to_pass(self) -> None:
        with mock.patch.object(mimo_audit, "log"):
            out, dropped = mimo_audit.filter_unanchored_findings(
                'VERDICT: ISSUES\n1. [quote: "never said this"] x', self.SLICE
            )
        self.assertEqual(out, "VERDICT: PASS")
        self.assertEqual(len(dropped), 1)

    def test_the_prompt_actually_asks_for_the_quote(self) -> None:
        # Without these two rules the filter is inert: every finding falls through as NOQUOTE.
        self.assertIn("[quote:", mimo_audit.AUDIT_PROMPT)
        self.assertIn("checked mechanically", mimo_audit.AUDIT_PROMPT)
        self.assertIn("A finding must assert a DEFECT", mimo_audit.AUDIT_PROMPT)


class ContentlessTurnTests(unittest.TestCase):
    def test_relays_acks_and_holds_are_skippable(self) -> None:
        for text in ("Idle.", "Holding.", "Standing by.", "Waiting.", "Noted.", "On it.",
                     "Visuals & World - FYI received; standby, no work started."):
            self.assertTrue(mimo_audit.contentless_turn(text, []), text)

    def test_anything_asserting_state_is_still_reviewed(self) -> None:
        for text in (
            "Live and trunk both 671e3aa",
            "Tests pass: 418/418",
            "PR #382 is open",
            "Deploy #88 running",
            "Done.",
            "Merged.",
            "3 checks green",
            "Applied the migration",
            "Standby, no work started; production is live.",
        ):
            self.assertEqual(mimo_audit.contentless_turn(text, []), "", text)

    def test_an_edit_or_a_long_answer_always_disqualifies(self) -> None:
        self.assertEqual(mimo_audit.contentless_turn("Idle.", ["src/a.ts"]), "")
        self.assertEqual(mimo_audit.contentless_turn("x" * (mimo_audit.CONTENTLESS_MAX_CHARS + 1), []), "")
        self.assertEqual(mimo_audit.contentless_turn("", []), "")


class NoiseClassTests(unittest.TestCase):
    def test_counter_classifies_without_raising(self) -> None:
        text = (
            "VERDICT: ISSUES\n1. [F1] confirm the run output before treating it as verified\n"
            "2. [F2] supporting output is elided in the slice\n"
            "3. [F3] counts changed 7 to 4 with no source change"
        )
        findings = [line for line in text.splitlines() if re.match(r"^\s*\d+[.)]\s+\S", line)]
        self.assertEqual(len(findings), 3)
        self.assertEqual(sum(1 for f in findings if mimo_audit.ADVISORY_RE.search(f)), 1)
        self.assertEqual(sum(1 for f in findings if mimo_audit.ELISION_RE.search(f)), 1)
        with mock.patch.object(mimo_audit, "log"):
            mimo_audit.log_noise_classes(text)


if __name__ == "__main__":
    unittest.main()

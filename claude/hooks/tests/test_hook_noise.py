"""2026-10-16 (Dom: are the hooks adding value or just noise?): two repeats the reviewer and the gate sent on every turn.

1. A lesson (an old reviewer finding) rode every prompt for up to a day even after the lane fixed it. It now retires as soon as the lane
   answers its finding ("[Fn] fixed:" or "rejected:"; only rejected ones were dropped before), else after MAX_LESSON_SHOWS prompts
   (Strategy's ruling, 2026-10-16: answered ID first, not time-based).
2. The "[Repository Quality Gate] deferred" notice remembered only the LAST reason per session, so two reasons that alternate (a deploy in
   flight, another gate already running) were each announced again every time; it now remembers the last few reasons.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import stop_orchestrator  # noqa: E402
from lib import autonomy, ledger  # noqa: E402


def _state(tmp: str):
    return (mock.patch.object(autonomy, "LESSONS_STATE", Path(tmp) / "state.json"),
            mock.patch.object(autonomy, "LESSONS_JSONL", Path(tmp) / "all.jsonl"))


def test_a_lesson_is_retired_after_it_has_been_shown_enough_times():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = _state(tmp)
        with a, b:
            autonomy.record_lesson("mimo_stop", '[quote: "CI is pending"] nothing checked it', "s1", "hooks")
            for shown in range(autonomy.MAX_LESSON_SHOWS):
                lessons = autonomy.active_lessons("hooks")
                assert len(lessons) == 1, f"shown {shown} times so far, still active"
                autonomy.note_lessons_shown(lessons)
            assert autonomy.active_lessons("hooks") == [], "retired after MAX_LESSON_SHOWS prompts"


def test_answering_a_finding_retires_its_lesson_whether_fixed_or_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = _state(tmp)
        with a, b, mock.patch.object(ledger, "LEDGER_PATH", Path(tmp) / "ledger.json"):
            verdict, _ = ledger.register_findings(
                "s1", 'VERDICT: ISSUES\n1. [quote: "CI pending"] nothing checked it ' + "x" * 200 +
                '\n2. [quote: "87%"] the figure has no source ' + "y" * 200 +
                '\n3. [quote: "merged"] left unanswered ' + "z" * 200)
            for line in verdict.splitlines()[1:]:
                autonomy.record_lesson("mimo_stop", line, "s1", "hooks")
            assert len(autonomy.active_lessons("hooks")) == 3
            ledger.apply_decisions("s1", "[F1] fixed: checked, green\n[F2] rejected: limit is the script's third column")
            for text in ledger.answered_texts("s1"):
                autonomy.forget_lesson(text)
            left = autonomy.active_lessons("hooks")
            assert len(left) == 1 and "left unanswered" in left[0], left
            assert [t[:40] for t in ledger.rejected_texts("s1")] == ['[quote: "87%"] the figure has no source '[:40]]


def test_a_new_occurrence_of_the_same_finding_teaches_again():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = _state(tmp)
        with a, b:
            text = '[quote: "87%"] the figure has no source'
            autonomy.record_lesson("mimo_stop", text, "s1", "hooks")
            for _ in range(autonomy.MAX_LESSON_SHOWS):
                autonomy.note_lessons_shown(autonomy.active_lessons("hooks"))
            assert autonomy.active_lessons("hooks") == []
            autonomy.record_lesson("mimo_stop", text, "s1", "hooks")   # the reviewer caught the same slip again
            assert autonomy.active_lessons("hooks") == [text], "a repeat of the mistake is a fresh lesson"


def test_showing_one_lesson_does_not_retire_the_others():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = _state(tmp)
        with a, b:
            autonomy.record_lesson("mimo_stop", "old `finding`", "s1", "hooks")
            for _ in range(autonomy.MAX_LESSON_SHOWS):
                autonomy.note_lessons_shown(autonomy.active_lessons("hooks"))
            autonomy.record_lesson("mimo_stop", "new `finding`", "s1", "hooks")
            assert autonomy.active_lessons("hooks") == ["new `finding`"]
            autonomy.note_lessons_shown(["a lesson nobody stored"])   # unknown text is ignored, never raises
            assert autonomy.active_lessons("hooks") == ["new `finding`"]


def test_the_deferral_notice_is_not_repeated_when_two_reasons_alternate():
    with tempfile.TemporaryDirectory() as tmp:
        notified = Path(tmp) / "notified.json"
        with mock.patch.object(stop_orchestrator, "DEFERRAL_NOTICE_PATH", notified), mock.patch.object(stop_orchestrator, "log"):
            stop_orchestrator._CONTEXT_NOTES.clear()
            deploy = {"revision": "6f94856aaa"}
            gate = {"reason": "a repository gate is already running in /x/y (pid 1, one gate per Mac)"}
            for deploying in (deploy, gate, deploy, gate, deploy):
                stop_orchestrator.defer_gate(deploying, "s1")
            notes = "\n".join(stop_orchestrator._CONTEXT_NOTES)
            assert notes.count("deploy 6f94856") == 1, "the deploy reason is announced once"
            assert notes.count("already running") == 1, "the other reason is announced once"


def test_the_deferral_notice_forgets_old_reasons_and_still_announces_new_ones():
    with tempfile.TemporaryDirectory() as tmp:
        notified = Path(tmp) / "notified.json"
        with mock.patch.object(stop_orchestrator, "DEFERRAL_NOTICE_PATH", notified), mock.patch.object(stop_orchestrator, "log"):
            stop_orchestrator._CONTEXT_NOTES.clear()
            for i in range(stop_orchestrator.DEFERRAL_NOTICE_MEMORY + 1):
                stop_orchestrator.defer_gate({"revision": f"{i:07d}"}, "s1")
            stop_orchestrator._CONTEXT_NOTES.clear()
            stop_orchestrator.defer_gate({"revision": f"{0:07d}"}, "s1")   # the oldest fell out of memory: announced again
            assert "deferred: deploy 0000000" in "\n".join(stop_orchestrator._CONTEXT_NOTES)
            stop_orchestrator._CONTEXT_NOTES.clear()
            stop_orchestrator.defer_gate({"revision": "9999999"}, "s1")
            assert "deploy 9999999" in "\n".join(stop_orchestrator._CONTEXT_NOTES), "a new reason is always announced"


def test_an_old_string_value_in_the_notice_file_still_counts_as_seen():
    with tempfile.TemporaryDirectory() as tmp:
        notified = Path(tmp) / "notified.json"
        notified.write_text('{"s1": "6f94856"}')      # the format written before this change
        with mock.patch.object(stop_orchestrator, "DEFERRAL_NOTICE_PATH", notified), mock.patch.object(stop_orchestrator, "log"):
            stop_orchestrator._CONTEXT_NOTES.clear()
            stop_orchestrator.defer_gate({"revision": "6f94856aaa"}, "s1")
            assert "\n".join(stop_orchestrator._CONTEXT_NOTES) == ""

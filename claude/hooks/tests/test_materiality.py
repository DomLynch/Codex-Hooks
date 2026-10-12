"""Dom, 2026-10-09 (via Strategy): the Stop reviewer is over-zealous on wording. Rule: on a turn with no diff, a finding
blocks only when the quoted claim is about the state of the world (live/merged/deployed/passed/fixed, PRs, CI, commits,
destructive actions). "Would Dom do something different if this word were exact?" Delivered vs queued, starting vs asked
to start, tense and attribution wording: logged, never blocked. A finding with no quote is kept (fails closed)."""
from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import mimo_audit as m  # noqa: E402

# Today's four wording blocks in Strategy's session (mimo_audit.log): must NOT block on a no-diff turn.
WORDING = [
    'work is starting now rather than waiting on you.',
    "Both are starting now, in parallel, and I've told Lead the dates go in the 09:00 table",
    "**Approved, Web is starting now:**",
    "you, the COO, Lead and Strategy are all aligned, and Hooks Dev has the go to ship it",
]
# The genuine catch the same day: stated as fact with no gh check. Must block.
MATERIAL = [
    "J is still in flight with four PRs open. No K PR exists yet.",
    "All four PRs pass their GitHub checks, but none has before/after stills in its description yet.",
    "The four PRs from my last entry (#1048, #1049, #1050 from Character Main, #1051) are all live.",
    "The fix is installed and the old process was killed.",
    "AssertionError [ERR_ASSERTION]: The input did not match the regular expression /^Replay/.",   # failed check, unreported
    "tests/test_gate_lanes.py:31: AssertionError",
    "const limb = draw < ARM_SHARE ? 1 : 2, mirror = draw >= .5;",   # code quoted on a no-diff turn
    "Saved the authority ruling to memory.",
]


def _verdict(*quotes):
    return "VERDICT: ISSUES\n" + "\n".join(f'{i}. [quote: "{q}"] Nothing in the slice backs this.' for i, q in enumerate(quotes, 1))


def test_wording_findings_do_not_block_a_no_diff_turn():
    for q in WORDING:
        text, dropped = m.filter_wording_findings(_verdict(q), no_diff=True)
        assert text == "VERDICT: PASS" and len(dropped) == 1, q


def test_material_claims_still_block_a_no_diff_turn():
    for q in MATERIAL:
        text, dropped = m.filter_wording_findings(_verdict(q), no_diff=True)
        assert text.startswith("VERDICT: ISSUES") and not dropped, q


def test_mixed_verdict_keeps_only_the_material_finding_renumbered():
    text, dropped = m.filter_wording_findings(_verdict(WORDING[0], MATERIAL[0]), no_diff=True)
    assert len(dropped) == 1 and text.splitlines()[1].startswith("1. ") and "PRs open" in text


def test_wording_never_downgrades_on_a_diff_turn():
    # 2026-10-12 replay: #117 downgraded "Want me to do both Monday?" on a diff turn (a premature stop). A diff turn always blocks.
    for q in WORDING:
        text, dropped = m.filter_wording_findings(_verdict(q), no_diff=False)
        assert text.startswith("VERDICT: ISSUES") and not dropped, q
    body = '1. [quote: "Want me to do both Monday?"] The work is not blocked, so it should not wait for permission or Monday.'
    assert not m.is_wording_finding(body, False)


def test_a_no_tool_turn_is_skipped_only_when_it_claims_nothing(tmp_path):
    # Strategy, option B (2026-10-10): no run / verification / deploy / pass-count / live / merged claim, no offer, no diff.
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    for msg in ("Sure, that works the way you described.", "Understood, I'll keep the wording plain.", "Yes, the two options differ in cost."):
        assert m.no_tool_calls([], msg, str(transcript)), msg
    for msg in ("I ran the test suite: 3310 pass. AG is live.", "Merged #116, all green.", "I verified it on GitHub.",
                "Release AA crashed the writer, so it is held on Z.", "Deployed a6d408cb4.", "412 passed, 0 failed.",
                "I can merge it if you like.", "Next: replace teh with the.", "The work is not yet complete."):
        assert not m.no_tool_calls([], msg, str(transcript)), msg   # a fabricated receipt or premature stop needs no tool call
    assert not m.no_tool_calls([], "", str(transcript))
    assert not m.no_tool_calls([], "Sure.", "")                         # no readable transcript: unknown, so review it
    assert not m.no_tool_calls([], "Sure.", str(tmp_path / "missing"))
    assert not m.no_tool_calls([("TOOL CALL", "Bash ls")], "Sure, that works.", str(transcript))


def test_state_verbs_are_never_wording_or_skipped():
    for msg in ("Release AA crashed the writer, so it is held on Z.", "It was rolled back and the box is down.",
                "The hook landed.", "Both were reinstalled."):
        assert m.STATUS_CLAIM_RE.search(msg), msg   # still a claim for the contentless-turn skip
    for diff in (False, True):
        assert not m.is_wording_finding('1. [quote: "the writer is held"] It is not held; it was reinstalled.', diff)
        assert not m.is_wording_finding('1. [quote: "Z crashed and was rolled back"] Not shown.', diff)


def test_blocks_on_numbers_ids_state_words_and_code_stay_exactly_as_before():
    both = ['[quote: "all 12 checks passed"] x', '[quote: "merged as db2ce77"] x', '[quote: "PR #116 is live"] x',
            '[quote: "it is green"] x']
    diff_only = ['[quote: "the fix handles it"] See `quality.py`.', '[quote: "it works"] Fails at line 40.',
                 '[quote: "we use it"] mimo_audit.py ignores it.', '[quote: "ran 12 tests"] x']
    for body in both:
        for no_diff in (True, False):
            assert not m.is_wording_finding(body, no_diff), (body, no_diff)
    for body in diff_only:
        assert not m.is_wording_finding(body, False), body


def test_replay_fixture_confusion_matrix_blocks_all_real_defects_and_passes_the_rest():
    """The 65 findings downgraded 2026-10-10..11, labelled by hand: real defect (A) must block; peer-restated (B) and minor
    wording (C) stay advisory on a no-diff turn. Row 14 is a diff-turn attribution row: it blocks after the #126 revert."""
    import json
    rows = json.loads((Path(__file__).parent / "wording_replay_2026_10_12.json").read_text())
    assert len(rows) == 65 and sum(r["class"] == "A" for r in rows) == 39
    cell = {"A blocked": 0, "A missed": 0, "B/C advisory": 0, "B/C blocked": 0}
    for r in rows:
        body = r["body"] + ('"' if r["body"].count('"') % 2 else "")   # the log truncated bodies at 400 chars; production gets the whole body
        advisory = m.is_wording_finding(body, r["turn"] == "nodiff")
        assert advisory == (r["expected"] == "advisory"), (r["id"], r["class"], r["body"][:100])
        key = ("A missed" if advisory else "A blocked") if r["class"] == "A" else ("B/C advisory" if advisory else "B/C blocked")
        cell[key] += 1
    print("confusion matrix", cell)
    assert cell == {"A blocked": 39, "A missed": 0, "B/C advisory": 22, "B/C blocked": 4}, cell   # blocked: row 14 (diff turn), rows 10, 16, 54


def test_whole_body_allowlist_any_sentence_that_is_not_attribution_blocks():
    # Auditor's 8 probes on #127 (2026-10-12): an attribution sentence plus a defect worded WITHOUT a keyword. Every one must block.
    base = '1. [quote: "Deploy is building it now."] The only support is your own SendMessage; no reply confirms it. {}'
    for extra in ("The deploy has stalled and nothing is moving.", "The work was never started.", "The check is red and hidden.",
                  "Nothing is watching the job.", "The count has no command behind it.", "It deleted nothing.",
                  "The tests were not run.", "The change is still unapplied.",
                  # the old keyword cues, still blocking because no sentence shape allows them
                  "Replace with: \"Deploy failed.\" The deploy failed at 16:49.", "Check the log: the deploy failed.",
                  "The run failed.", "That is false.", "It contradicts the record.", "Do it Monday.", "Run it now."):
        assert not m.is_wording_finding(base.format(extra), True), extra
    assert m.is_wording_finding(base.format(""), True)
    # advice after "Replace with / Say instead" is not part of the finding
    assert m.is_wording_finding(base.format('Replace it with: "I asked Deploy to build it; it has not failed so far."'), True)
    # numbered bodies work (Auditor LOW: the first branch used to be anchored on "[")
    assert m.is_wording_finding('3. [quote: "Web is starting now"] Nothing in the slice backs this.', True)
    # Strategy's rule stays: a state word, number or id in the quote keeps blocking
    assert not m.is_wording_finding('1. [quote: "Deploy merged PR #12 and it is live."] The only support is your own SendMessage.', True)

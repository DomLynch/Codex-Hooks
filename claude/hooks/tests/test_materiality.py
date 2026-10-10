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


def test_wording_does_not_block_a_diff_turn_but_numbers_and_code_do():
    text, dropped = m.filter_wording_findings(_verdict(WORDING[0]), no_diff=False)
    assert text == "VERDICT: PASS" and len(dropped) == 1
    for q in ("seven tests passed", "ran 12 tests", "it handles the empty case"):
        body = f'1. [quote: "{q}"] See `quality.py` line 40.' if "empty" in q else f'1. [quote: "{q}"] Not shown.'
        text, dropped = m.filter_wording_findings("VERDICT: ISSUES\n" + body, no_diff=False)
        assert text.startswith("VERDICT: ISSUES") and not dropped, q
    text, dropped = m.filter_wording_findings(_verdict(*MATERIAL), no_diff=False)
    assert len(text.splitlines()) - 1 == len(MATERIAL) and not dropped


def test_no_tool_turn_without_claims_is_skipped_but_claims_and_tools_are_reviewed():
    assert m.no_tools_no_claims([], "Sure, that works the way you described.")
    assert not m.no_tools_no_claims([], "Merged #116, all green.")
    assert not m.no_tools_no_claims([("TOOL CALL", "Bash ls")], "Sure, that works.")
    assert not m.no_tools_no_claims([], "I can merge it if you like.")


def test_unquoted_finding_is_kept():
    text, dropped = m.filter_wording_findings("VERDICT: ISSUES\n1. Something is off with the wording.", no_diff=True)
    assert text.startswith("VERDICT: ISSUES") and not dropped


def test_state_verbs_are_never_wording_or_skipped():
    for msg in ("Release AA crashed the writer, so it is held on Z.", "It was rolled back and the box is down.",
                "The hook landed.", "Both were reinstalled."):
        assert m.no_tools_no_claims([], msg) == "", msg
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

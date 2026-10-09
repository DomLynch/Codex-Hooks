"""Dom via Strategy, 2026-10-09: three slips reached Dom ("copying the Pit's code", "the Pit's own animations", "being
built"); a lane briefed with "copy" will copy. Log-only (never a block): when a lane's outgoing message to another lane
touches src/fight/ or origins/combat/ and uses a banned word, one BANNED_WORDING line. Allow-listed fixtures never hit."""
from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib.banned_words import banned_engine_words  # noqa: E402


def _send(text):
    return [("TOOL CALL", 'SendMessage: [t1] {"to": "Combat", "message": "%s"}' % text)]


def test_the_three_slips_are_caught_on_engine_slice_work():
    for text in ("copying the Pit's code into src/fight/arena.ts", "the Pit's own animations in src/fight/ are reused",
                 "origins/combat/ finishers are being built next", "port the lunge to origins/combat/", "rebuild src/fight/hit.ts"):
        assert banned_engine_words(_send(text)), text


def test_allowed_words_and_fixtures_do_not_hit():
    for text in ("move the lunge into src/fight/arena.ts", "extract and rename in origins/combat/",
                 "the engine's hit model in src/fight/", "the Pit client reads src/fight/state",
                 "Pit-only toggle in src/fight/", "the Pit's live circle (wall) in src/fight/ stays"):
        assert not banned_engine_words(_send(text)), text


def test_banned_words_outside_engine_slice_do_not_hit():
    assert not banned_engine_words(_send("copy the README and build the docs site"))


def test_only_outgoing_messages_are_checked():
    assert not banned_engine_words([("TOOL CALL", "Bash: [t2] cp src/fight/a.ts b.ts  # copy"), ("ASSISTANT", "copying src/fight/")])

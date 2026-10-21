"""cache_guard fails CLOSED on a path it cannot resolve (Strategy + Dom, 2026-10-21).

On 2026-10-21 a cleanup loop deleted ~/Library/Caches/ms-playwright through a shell variable:
    for s in "$HOME/.cache/uv" "$HOME/Library/Caches/ms-playwright" ...; do ... rm -rf "${s:?}" ...; done
cache_guard skipped every target containing `$` (fail-open), so it never fired, and the release train's Mac browser rows failed.
An inline literal assignment or for-loop in the same command is resolved; anything still unknown is denied when it could match the cache
or a folder that holds it, and allowed when a literal part rules it out (`$DIR/node_modules`)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as g  # noqa: E402

CWD = "/Users/x/Developer/frankendom-deploy"

# the exact shape that slipped through at 10:xx
SLIPPED = ('B=$(df -k / | tail -1); for s in "$HOME/.cache/uv" "$HOME/Library/Caches/ms-playwright" "$HOME/Library/Caches/pip"; do '
           'if [ -d "$s" ]; then sz=$(du -sh "$s" | cut -f1); rm -rf "${s:?}" && echo "removed $sz  $s"; fi; done')

DENY = [
    SLIPPED,
    'for s in "$HOME/Library/Caches/ms-playwright"; do rm -rf "$s"; done',
    's="$HOME/Library/Caches/ms-playwright"; rm -rf "$s"',
    's=~/Library/Caches; rm -rf ${s}',
    'D=/root/.cache; ssh root@49.12.7.18 "rm -rf /root/.cache/ms-playwright"',
    'rm -rf "${s:?}"',                        # nothing says what s is: it could be the cache or its parent
    'rm -rf "$target"',
    'rm -rf $(mktemp -d)',
    'rm -rf "$(cat /tmp/list)"',
    'mv "$a" /tmp/elsewhere',
    'rsync -a --delete /tmp/empty/ "$DEST/"',
    'rsync -a --delete /tmp/empty/ ~/Library/Caches/ms-playwright/',
    'rsync -a --delete /tmp/empty/ root@49.12.7.18:/root/.cache/ms-playwright/',
]
ALLOW = [
    'for s in "$HOME/.cache/uv" "$HOME/Library/Caches/pip"; do rm -rf "${s:?}"; done',
    's="$HOME/Library/Caches/semble"; rm -rf "$s"',
    's=~/.cache/uv; rm -rf "$s"',
    'rm -rf "$DIR/node_modules"',
    'rm -rf "${DIR:?}/dist"',
    'rm -rf $UNSET_VAR/x',
    'rsync -a /tmp/src/ "$DEST/"',            # no --delete: nothing is removed at the destination
    'rsync -a --delete /tmp/empty/ /tmp/scratch/',
    'rm -f "$HOME/.cache/uv/x"',
    'ls "$s"',
    'du -sh "$s"',
]


def test_denies_variable_targets_that_could_be_the_cache_or_a_parent():
    missed = [c for c in DENY if not g.cache_delete_reason(c, CWD)]
    assert not missed, missed


def test_the_exact_command_that_slipped_through_is_denied_with_the_literal_path_message():
    reason = g.cache_delete_reason(SLIPPED, CWD)
    assert reason and "ms-playwright" in reason


def test_unresolvable_path_says_use_the_literal_path():
    reason = g.cache_delete_reason('rm -rf "${s:?}"', CWD)
    assert "unresolvable path" in reason and "literal path" in reason


def test_resolved_or_ruled_out_variables_stay_allowed():
    wrongly = [c for c in ALLOW if g.cache_delete_reason(c, CWD)]
    assert not wrongly, wrongly

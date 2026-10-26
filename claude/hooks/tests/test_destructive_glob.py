"""rm on a relative glob after a `cd` is refused with a rewrite recipe, never left to Claude Code's approval card (Dom, 2026-10-26).

An OpenTrident lane ran `cd <scratchpad>/grip; rm o2/*; node shot.mjs ...`; the app raised "Dangerous rm operation on statically-unresolvable target:
.../o2/*" in front of Dom, in bypass mode too, and the lane sat idle until he looked (it resolved o2/* against the session folder, not the cd target).
The variable shapes were already refused (#173); this one was not. An absolute glob (`rm -f /tmp/a/*.log`) stays allowed (decided 2026-10-21).
The refusal must tell the session what to do instead, so it rewrites and carries on in the same turn."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.destructive_var_guard import variable_delete_reason as reason  # noqa: E402

DENY = [
    "cd /private/tmp/x/scratchpad/grip; rm o2/*; node shot.mjs j2.json o2 t0.json && node sheet.mjs s2.jpg 4 375 o2/*.png",
    "cd /tmp/a; rm o2/*",
    "cd /x && rm -f *.log",
    "pushd /x; rm build/*",
    "cd /tmp/a; rm -rf build/*",
    "cd /tmp/a; rm -f -- file?.txt",
    "cd /tmp/a; rm [ab].txt",
    "cd /tmp/a; unlink o2/*",
    "echo go; cd /tmp/a && echo x && rm o2/*.png",
    "bash -c 'cd /tmp/a && rm *.tmp'",
    "ssh root@49.12.7.18 'rm -f scratch/*'",
    "ssh root@49.12.7.18 'cd /root/scratch && rm -f *.png'",
]
ALLOW = [
    "rm -f /Users/x/Developer/repo/out/*.png",          # absolute glob: decided 2026-10-21
    "rm -f /tmp/a/*.log",
    "ssh root@49.12.7.18 'rm -f /root/scratch/*'",
    "cd /tmp/a; rm -f /tmp/a/o2/*.png",
    "rm o2/*",                                          # no cd: the app resolves it against the session folder
    "cd /tmp/a; rm -rf /tmp/a/o2 && mkdir -p /tmp/a/o2",
    "cd /tmp/a; rm -f o2/a.png",
    "cd /tmp/a; rm -- a.png b.png",
    'cd /tmp/a; rm -f "o2/literal*star.txt"',
    "cd /tmp/a; rm -f 'o2/literal*star.txt'",
    "cd /tmp/a; ls o2/*",
    "cd /tmp/a; echo *",
    "cd /tmp/a; find /tmp/a/o2 -maxdepth 1 -type f -name '*.png' -delete",
    "cd /tmp/a; mv *.png /tmp/b/",
    "cd /tmp/a; cat o2/*.json | wc -l",
    "cd /tmp/a; grep -r 'rm o2/*' docs",
]


@pytest.mark.parametrize("command", DENY)
def test_rm_on_a_relative_glob_after_cd_is_refused(command):
    assert reason(command) != ""


@pytest.mark.parametrize("command", ALLOW)
def test_literal_absolute_and_other_glob_uses_are_not_refused(command):
    assert reason(command) == ""


def test_the_refusal_says_what_to_do_instead_so_the_lane_keeps_going():
    text = reason("cd /tmp/a; rm o2/*")
    assert "popup" in text and "rm -rf" in text and "mkdir -p" in text
    assert "SAME turn" in text and "find" in text and "no question to Dom" in text

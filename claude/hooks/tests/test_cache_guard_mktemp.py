"""cache_guard: a variable assigned from a plain `mktemp` is a brand-new path, so deleting it is not an unresolved delete (Lead, Frankendom V1, 2026-10-26).

scripts/deploy.sh does `svc=$(mktemp) && chmod 600 "$svc"` and later `rm -f "$svc"`. The script-file scan (2026-10-22) read the `rm -f "$svc"`
as an unresolvable path and refused every `bash scripts/deploy.sh`, which blocked the V1 release. mktemp always creates a NEW name, so it can never be
the cache or a folder that holds it, EXCEPT when it is told where to create it (`-p DIR`, `--tmpdir=DIR`, a template containing `/`): those stay unknown."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as g  # noqa: E402

CWD = "/Users/x/Developer/frankendom-deploy"

ALLOW = [
    'svc=$(mktemp) && chmod 600 "$svc"; rm -f "$svc"',
    't=$(mktemp -d); rm -rf "$t"',
    'f=`mktemp`; rm -f "$f"',
    't=$(mktemp -d /tmp/fkXXXXXX); rm -rf "$t"',
    'svc=$(mktemp)\npsql -c x\nrm -f "$svc"',
]
DENY = [
    'd=$(mktemp -p ~/Library/Caches/ms-playwright); rm -rf "$d"',
    'd=$(mktemp -d --tmpdir=$HOME/Library/Caches); rm -rf "$d"',
    'd=$(mktemp ~/Library/Caches/ms-playwrightXXXX); rm -rf "$d"',
    'd=$(mktemp); d=$(cat /tmp/x); rm -rf "$d"',
    'd=$(mktemp -d); rm -rf "$d/../../.."',
    'rm -rf $(mktemp -d)',
    'rm -rf "$d"',
]


@pytest.mark.parametrize("command", ALLOW)
def test_variable_from_plain_mktemp_is_resolvable(command):
    assert g.cache_delete_reason(command, CWD) == ""


@pytest.mark.parametrize("command", DENY)
def test_mktemp_with_a_destination_or_an_unknown_variable_stays_denied(command):
    assert g.cache_delete_reason(command, CWD) != ""


def test_a_script_file_with_the_deploy_sh_shape_is_not_refused(tmp_path):
    script = tmp_path / "deploy.sh"
    script.write_text('#!/usr/bin/env bash\nset -e\nsvc=$(mktemp) && chmod 600 "$svc"\nrm -f "$svc"\n')
    assert g.cache_delete_reason("bash " + str(script), str(tmp_path)) == ""


def test_a_script_file_that_deletes_an_unknown_variable_is_still_refused(tmp_path):
    script = tmp_path / "bad.sh"
    script.write_text('#!/usr/bin/env bash\nrm -rf "$target"\n')
    assert g.cache_delete_reason("bash " + str(script), str(tmp_path)) != ""

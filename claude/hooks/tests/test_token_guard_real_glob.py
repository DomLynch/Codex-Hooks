"""token_guard: a broad dot-glob over a LOCAL folder is judged by what the shell would expand it to (Dom via Strategy, 2026-10-23).

False block found 2026-10-23: `cd ~; for d in * .[a-zA-Z]*; do du -xsk "$d"; done` was denied on the Mac because `.[a-zA-Z]*` COULD match `.hf`,
although ~/.hf does not exist there (the token lives only on the VPS at /root/.hf). The pattern was compared with the token folder's name in
the abstract and never looked at the filesystem.

Strategy ruling (option A): for a LOCAL command only, expand a glob against the real filesystem exactly as the shell would (cwd taken after any
leading `cd`; a cwd that cannot be worked out statically fails closed), and deny only when a real match touches the token folder. Everything
that cannot be seen from here stays fail-closed as before: ssh and bash -c strings, the remote half of scp/rsync, substitutions, quote splits,
and a glob whose literal prefix is already the token folder (`/root/.hf/tok*`, the Auditor's 10-21 case)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import token_guard as g  # noqa: E402

LOOP = 'cd ~; for d in * .[a-zA-Z]*; do [ -e "$d" ] && du -xsk "$d" 2>/dev/null; done | sort -rn | head -14'


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake HOME with ordinary dot-folders and no ~/.hf, as on the Mac."""
    for name in (".cache", ".claude", ".npm", "Developer"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(g, "HF_ROOTS", [str(tmp_path / ".hf"), "/root/.hf"])
    return tmp_path


def _reason(command, home, cwd=None):
    return g.hf_token_reason(command, str(cwd or home), str(home))


def test_dot_glob_over_a_folder_without_the_token_folder_is_allowed(home):
    assert not _reason(LOOP, home)
    assert not _reason("du -xsk .[a-zA-Z]*", home)
    assert not _reason("cd ~ && du -sk .[a-z]* .??*", home)
    assert not _reason("du -sk ~/.[a-z]*", home, cwd="/tmp")
    assert not _reason("du -sk ~/.c*", home)


def test_the_same_glob_is_denied_once_the_token_folder_exists(home):
    (home / ".hf").mkdir()
    (home / ".hf" / "token").write_text("x")
    assert _reason(LOOP, home)
    assert _reason("du -xsk .[a-zA-Z]*", home)
    assert _reason("cd ~ && cat .h*/token", home)
    assert _reason("du -sk ~/.h*", home, cwd="/tmp")


def test_cwd_comes_from_the_leading_cd_not_the_session_folder(home):
    (home / ".hf").mkdir()
    elsewhere = home / "Developer"
    assert _reason("cd ~ && du -sk .[a-z]*", home, cwd=elsewhere)         # cd puts us in the folder that holds .hf
    assert not _reason("du -sk .[a-z]*", home, cwd=elsewhere)              # no cd: Developer has no dot-folders


def test_a_cwd_that_cannot_be_worked_out_fails_closed(home):
    for command in ('cd "$ELSEWHERE" && du -sk .[a-z]*', "cd $(pwd) && du -sk .[a-z]*", "cd - && du -sk .[a-z]*",
                    "pushd /tmp && du -sk .[a-z]*", "(cd /tmp; du -sk .[a-z]*)"):
        assert _reason(command, home), command


def test_what_cannot_be_seen_from_here_stays_closed(home):
    for command in ("cat /root/.hf/tok*", "cat /root/.hf/*", "du -sk /root/.hf*",
                    "ssh root@49.12.7.18 'du -sk /root/.[a-z]*'", 'ssh root@49.12.7.18 "cd /root && du -sk .[a-z]*"',
                    "scp root@49.12.7.18:/root/.[a-z]* /tmp/", "rsync -a root@49.12.7.18:/root/.h* /tmp/",
                    "bash -c 'cd /root && cat .h*/token'", "du -sk $(printf /roo)t/.hf", "du -sk `echo /roo`t/.hf",
                    "cat /root/.hf/to''ken", 'cat "/root/.hf/to"ken'):
        assert _reason(command, home), command


def test_an_expansion_too_long_to_judge_falls_back_to_the_abstract_check(home, monkeypatch):
    """Auditor, 2026-10-23: the list was cut at 5000, so a token folder sorting after the cut was never looked at (failed open)."""
    monkeypatch.setattr(g, "GLOB_CAP", 3)
    for i in range(10):
        (home / f".aa{i}").mkdir()
    assert _reason("du -sk .[a-z]*", home)            # 12+ matches > cap: judged in the abstract, `.[a-z]*` could be `.hf`
    assert not _reason("du -sk .aa[0-1]", home)       # 2 matches, under the cap, none is the token folder


def test_a_literal_token_folder_is_still_denied_locally(home):
    for command in ("du -sk ~/.hf", "cat ~/.hf/token", "cp -r ~/.hf /tmp/x", "cat $HOME/.hf/token"):
        assert _reason(command, home), command

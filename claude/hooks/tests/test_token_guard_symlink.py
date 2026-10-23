"""token_guard, tier 1 of the Auditor's holes (Strategy ruling A, 2026-10-23): the two that are cheap and real.

  - a SYMLINK to the token folder is the token folder: `cat ~/lnk/token`, `cd ~/lnk && cat *`, `cp -r ~/lnk /tmp`.
  - a relative word that starts with `*` (`cat *`) must be expanded in the folder the command `cd`ed into, not in the hook process's own folder
    (a bug of the real-glob change #176: _resolve leaves such a word unjoined because a `*` can stand for an unknown variable).
The exotic spellings (dotglob option names, zsh qualifiers, braces, ANSI-C, case) are tier 2: listed, not pursued."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import token_guard as g  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake HOME holding the token folder, a symlink to it, a symlink chain, and a work folder with no dot-folders."""
    real = tmp_path.resolve()
    (real / ".hf").mkdir()
    (real / ".hf" / "token").write_text("x")
    (real / "work").mkdir()
    (real / "work" / "a.txt").write_text("a")
    (real / "lnk").symlink_to(real / ".hf")
    (real / "lnk2").symlink_to(real / "lnk")
    monkeypatch.setattr(g, "HF_ROOTS", [str(real / ".hf"), "/root/.hf"])
    return real


def _reason(command, home, cwd=None):
    return g.hf_token_reason(command, str(cwd or home), str(home))


def test_a_symlink_to_the_token_folder_is_the_token_folder(home):
    for command in ("cat ~/lnk/token", "cat ~/l*/token", "cd ~/lnk && cat token", "cd ~ && cat lnk/*", "cp -r ~/lnk /tmp/x",
                    "cd ~/lnk && tar czf /tmp/x.tgz .", "cat ~/lnk2/token", "cd ~/lnk2 && cat token"):
        assert _reason(command, home), command
    assert not _reason("ls ~/lnk", home)   # ls reads no content


def test_a_bare_star_is_expanded_in_the_folder_the_command_cded_into(home):
    assert _reason("cd ~/lnk && cat *", home, cwd=home / "work")        # the cd'd folder holds `token` behind a symlink
    assert _reason("cd ~/lnk2 && cat *", home, cwd=home / "work")
    assert _reason("cd ~/lnk && cat ./*", home, cwd=home / "work")
    assert not _reason("cd ~/work && cat *", home, cwd=home)            # same word, a folder with nothing behind it
    assert not _reason("cat *", home, cwd=home / "work")


def test_ordinary_commands_stay_allowed(home):
    work = home / "work"
    for command in ("cat a.txt", "du -sk *", "git add *", "ls -la", "cd ~/work && wc -l *", "tar czf /tmp/x.tgz *", "cat ~/work/a.txt"):
        assert not _reason(command, home, cwd=work), command

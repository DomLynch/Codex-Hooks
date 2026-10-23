"""token_guard: the false ALLOWS the Auditor found while passing #176 (2026-10-23). They are older than #176 (main allowed them too), and a false allow
in a token guard outranks a false block, so Strategy ruled them first.

  - a shell option that makes a glob match dot-folders: `shopt -s dotglob`, `setopt globdots`, `GLOBIGNORE=.`  -> `cat ~/?hf/token` reads the folder
  - a zsh glob qualifier or a bash extglob group in a word: `cat ~/?hf(D)/token`, `cat ~/@(.hf)/token`
  - `[^..]` is a negation in bash and zsh, but fnmatch reads the `^` as a letter: `cat .[^c]*/token`
  - a symlink to the token folder, in the path or as the cwd: `cd ~/lnk && cat *`, `cat ~/l*/token`, `cat ~/lnk/token`

Rule: when the command changes how globs treat dot-folders, or a word carries a qualifier, a glob that COULD name the token folder is denied
without looking at the disk (fail closed); `[^` means `[!`; every local path is also judged after its symlinks are resolved."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import token_guard as g  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake HOME that HOLDS the token folder, a symlink to it, and ordinary dot-folders."""
    real = tmp_path.resolve()
    (real / ".hf").mkdir()
    (real / ".hf" / "token").write_text("x")
    for name in (".cache", ".claude", "Developer"):
        (real / name).mkdir()
    (real / "lnk").symlink_to(real / ".hf")
    monkeypatch.setattr(g, "HF_ROOTS", [str(real / ".hf"), "/root/.hf"])
    return real


def _reason(command, home, cwd=None):
    return g.hf_token_reason(command, str(cwd or home), str(home))


def test_an_option_that_makes_globs_match_dot_folders_closes_the_glob(home):
    for command in ("shopt -s dotglob; cat ~/?hf/token", "shopt -s dotglob; cat ~/*hf/token", "shopt -s dotglob; cat ~/*f/token",
                    "GLOBIGNORE=.; cat ~/*f/token", "GLOBIGNORE='.:..'; cat ~/*f/token", "setopt globdots; cat ~/?hf/token",
                    "setopt GLOB_DOTS && cat ~/?hf/token", "shopt -s dotglob && cd ~ && cat ?hf/token"):
        assert _reason(command, home), command


def test_the_option_closes_the_glob_even_when_the_folder_is_not_there(home):
    (home / ".hf" / "token").unlink()
    (home / ".hf").rmdir()
    for command in ("shopt -s dotglob; cat ~/?hf/token", "setopt globdots; cat ~/*f/token", "GLOBIGNORE=.; cat ~/*f/token"):
        assert _reason(command, home), command


def test_a_qualifier_or_extglob_group_in_a_word_closes_the_glob(home):
    for command in ("cat ~/?hf(D)/token", "cat ~/*hf(D)/token", "cat ~/@(.hf)/token", "shopt -s extglob; cat ~/+(.hf)/token"):
        assert _reason(command, home), command


def test_caret_negation_is_read_as_the_shell_reads_it(home):
    assert _reason("cd ~ && cat .[^c]*/token", home)
    assert _reason("cat ~/.[^c]*/token", home, cwd="/tmp")
    assert not _reason("cd ~ && du -sk .[^h]*", home)          # `[^h]` excludes .hf: nothing here touches the token folder


def test_a_symlink_to_the_token_folder_is_the_token_folder(home):
    for command in ("cat ~/lnk/token", "cat ~/l*/token", "cd ~/lnk && cat token", "cd ~/lnk && cat *", "cd ~ && cat lnk/*",
                    "cp -r ~/lnk /tmp/x", "cd ~/lnk && tar czf /tmp/x.tgz ."):
        assert _reason(command, home), command
    assert not _reason("ls ~/lnk", home)                       # ls reads no content


def test_ordinary_globs_stay_allowed_when_the_folder_exists_and_no_option_is_set(home):
    for command in ("cd ~ && du -sk .c*", "cd ~ && du -sk .cl*", "shopt -s nullglob; cd ~ && du -sk .c*", "cd ~ && du -sk Dev*",
                    "git commit -m 'fix bug(s) in the loop' && git add *", "echo 'a(b)' && du -sk *", "cd ~/Developer && ls *.py"):
        assert not _reason(command, home), command

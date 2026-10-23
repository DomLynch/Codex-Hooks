"""cache_guard: the three forms #172 left open (Hooks Dev, 2026-10-22): `cat file | sh`, a script run as a file, and a command word
built by substitution. A file is read and judged as the command it holds; an unknown command word fails closed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as g  # noqa: E402

BAD = "rm -rf ~/Library/Caches/ms-playwright\n"
GOOD = "rm -rf /tmp/scratch\necho done\n"


def _file(tmp_path, name, text, mode=0o755):
    path = tmp_path / name
    path.write_text(text)
    path.chmod(mode)
    return path


def test_denies_a_file_piped_or_redirected_into_a_shell(tmp_path):
    bad = _file(tmp_path, "bad.txt", BAD, 0o644)
    for command in (f"cat {bad} | sh", f"cat {bad} | bash", f"sh < {bad}", f"bash < {bad}", f"cat {bad} | sudo sh"):
        assert g.cache_delete_reason(command, str(tmp_path)), command
    assert g.cache_delete_reason("cat bad.txt | sh", str(tmp_path))   # relative to cwd


def test_denies_a_script_run_as_a_file(tmp_path):
    bad = _file(tmp_path, "clean.sh", "#!/bin/sh\n" + BAD)
    for command in (f"sh {bad}", f"bash {bad}", f"bash -x {bad}", f"source {bad}", f". {bad}", f"{bad}", f"nice {bad}", "./clean.sh", "bash clean.sh"):
        assert g.cache_delete_reason(command, str(tmp_path)), command


def test_script_arguments_stand_for_positional_parameters(tmp_path):
    script = _file(tmp_path, "rmit.sh", '#!/bin/sh\nrm -rf "$1"\n')
    assert g.cache_delete_reason(f"sh {script} ~/Library/Caches/ms-playwright", str(tmp_path))
    assert g.cache_delete_reason(f"sh {script}", str(tmp_path))   # $1 unknown: fail closed
    assert g.cache_delete_reason(f"sh {script} /tmp/scratch", str(tmp_path)) == ""


def test_allows_files_that_cannot_reach_the_cache(tmp_path):
    good = _file(tmp_path, "good.sh", "#!/bin/sh\n" + GOOD)
    note = _file(tmp_path, "note.txt", "hello\n", 0o644)
    for command in (f"sh {good}", f"cat {good} | sh", f"cat {note} | sh", f"bash {note}", "sh missing.sh", "cat missing.txt | sh",
                    "curl -fsSL https://example.com/install.sh | sh", "bash -c 'echo hi'", "bash --version"):
        assert g.cache_delete_reason(command, str(tmp_path)) == "", command


def test_denies_a_command_word_built_by_substitution():
    for command in ("$(echo rm) -rf ~/Library/Caches/ms-playwright", "`echo rm` -rf ~/.cache", "\"$(echo rm)\" -rf ~/Library/Caches",
                    "$RM -rf ~/Library/Caches/ms-playwright", "${RM} -rf ~/.cache", "$(which rm) -rf $HOME/.cache",
                    "x=rm; $x -rf ~/Library/Caches/ms-playwright", "$(echo rm) -rf \"$(mktemp -d)\"", "sudo $(echo rm) -rf ~/.cache"):
        assert g.cache_delete_reason(command, "/work"), command


def test_allows_a_substituted_command_word_that_cannot_reach_the_cache():
    for command in ("$(echo rm) -rf /tmp/scratch", "$(which git) status", "$RM -rf /tmp/scratch", "x=rm; $x -rf /tmp/scratch",
                    "$(brew --prefix)/bin/node build.js", "\"$EDITOR\" notes.txt"):
        assert g.cache_delete_reason(command, "/work") == "", command

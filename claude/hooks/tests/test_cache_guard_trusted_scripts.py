"""cache_guard: a TRACKED, UNMODIFIED script in a git checkout under ~/Developer is judged on literal and resolved paths only (Strategy ruling B, 2026-10-26).

The V1 release (scripts/deploy.sh, lib/deploy-lock.sh, post-publish.sh ...) went through PRs but deletes through variables (`rm -rf "$1"`, `rm -f "$DEPLOY_LOCK"`), so the
fail-closed rule for script FILES (#175) refused every deploy. Trust is narrow: untracked, modified, outside ~/Developer and every inline command stay fail-closed,
and a literal never-touch target is denied even in a trusted script (the 10-21 incident shape: an ad-hoc loop with $s on the command line)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as g  # noqa: E402

SCRIPT = '#!/usr/bin/env bash\nset -e\nsvc=$(mktemp)\nrm -f "$svc"\nrm -rf "$1"\nrm -f "$DEPLOY_LOCK"\nfor s in a b; do rm -rf "${s:?}"; done\n'


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


def make_repo(home: Path, name: str = "repo", body: str = SCRIPT, commit: bool = True) -> Path:
    repo = home / "Developer" / name
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q")
    script = repo / "scripts" / "deploy.sh"
    script.write_text(body)
    if commit:
        git(repo, "add", "scripts/deploy.sh")
        git(repo, "commit", "-q", "-m", "x")
    return repo


def reason(repo: Path, home: Path, command: str = "bash scripts/deploy.sh") -> str:
    return g.cache_delete_reason(command, str(repo), str(home))


def test_tracked_unmodified_script_with_unknown_variables_is_allowed(tmp_path):
    repo = make_repo(tmp_path)
    assert reason(repo, tmp_path) == ""


def test_untracked_script_stays_fail_closed(tmp_path):
    repo = make_repo(tmp_path, commit=False)
    assert reason(repo, tmp_path) != ""


def test_modified_script_stays_fail_closed(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "scripts" / "deploy.sh").write_text(SCRIPT + "# edited after the PR\n")
    assert reason(repo, tmp_path) != ""


def test_staged_but_uncommitted_edit_stays_fail_closed(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "scripts" / "deploy.sh").write_text(SCRIPT + "echo changed\n")
    git(repo, "add", "scripts/deploy.sh")
    assert reason(repo, tmp_path) != ""


def test_tracked_script_outside_developer_is_not_trusted(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "elsewhere"
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "scripts" / "deploy.sh").write_text(SCRIPT)
    git(repo, "add", "scripts/deploy.sh")
    git(repo, "commit", "-q", "-m", "x")
    assert reason(repo, home) != ""


def test_a_literal_never_touch_target_is_denied_even_in_a_trusted_script(tmp_path):
    literal = Path.home() / "Library" / "Caches" / "ms-playwright"
    repo = make_repo(tmp_path, name="lit", body='#!/usr/bin/env bash\nrm -rf "%s"\n' % literal)
    assert "ms-playwright" in reason(repo, tmp_path)


def test_a_full_line_comment_naming_a_script_is_not_a_command(tmp_path):
    body = '#!/usr/bin/env bash\n# The presence service (ops/install-presence.sh) flips with the site\necho ok\n'
    repo = make_repo(tmp_path, name="cmt", body=body)
    (repo / "ops").mkdir()
    (repo / "ops" / "install-presence.sh").write_text('rm -rf "%s"\n' % (Path.home() / "Library" / "Caches" / "ms-playwright"))
    assert reason(repo, tmp_path) == ""


def test_the_1021_incident_shape_stays_denied_as_an_inline_command(tmp_path):
    repo = make_repo(tmp_path)
    loop = 'for s in "$HOME/.cache/uv" "$HOME/Library/Caches/ms-playwright"; do rm -rf "${s:?}"; done'
    assert g.cache_delete_reason(loop, str(repo)) != ""   # real home: the protected paths are the real ones
    assert g.cache_delete_reason('rm -rf "$target"', str(repo)) != ""

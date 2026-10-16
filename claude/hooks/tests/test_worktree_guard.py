"""Lead + Dom, 2026-10-16 ("a universal fix so this wastage doesn't happen again"): the Mac hit 94% disk with 65 finished side copies
(~70 GB) of ~/Developer/frankendom-main. Strategy's ruling the same day: ONE side worktree per lane (its lane folder plus at most one
per-task side copy, removed with the PR). `git worktree add ~/Developer/frankendom-<lane>-<x>` is refused when that lane already has one;
Dom's two sandboxes and frankendom-main are not lanes for this count (RPG-game AGENTS.md, PR #2874)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard  # noqa: E402
from lib import worktree_guard as g  # noqa: E402

DEV = str(Path.home() / "Developer")
LANE = f"{DEV}/frankendom-combat"
ONE = [f"{DEV}/frankendom-combat-gt"]
MESSAGE = "remove your previous side worktree first (one per lane, Strategy 2026-10-16)"


def copies(paths):
    return lambda prefix: [p for p in paths if Path(p).name.startswith(prefix)]


def reason(command, cwd=LANE, existing=ONE):
    return g.worktree_add_reason(command, cwd, copies(existing))


def test_a_second_side_copy_is_denied():
    for cmd in (
        f"git worktree add {DEV}/frankendom-combat-new -b combat/new origin/main",
        "git worktree add ~/Developer/frankendom-combat-new -b combat/new origin/main",
        "git worktree add $HOME/Developer/frankendom-combat-new origin/main",
        'git worktree add "$HOME/Developer/frankendom-combat-new" origin/main',
        "git worktree add ${HOME}/Developer/frankendom-combat-new origin/main",
        f"git worktree add --detach {DEV}/frankendom-combat-new origin/main",
        f"git worktree add -f -b b1 {DEV}/frankendom-combat-new",
        f"git -C {DEV}/frankendom-main worktree add {DEV}/frankendom-combat-new -b x origin/main",
        f"cd {DEV}/frankendom-main && git worktree add {DEV}/frankendom-combat-new origin/main && echo done",
        f"FOO=1 git worktree add {DEV}/frankendom-combat-new",
        "git worktree add ../frankendom-combat-new origin/main",
    ):
        assert reason(cmd).startswith(MESSAGE), cmd


def test_the_lane_comes_from_the_cwd_even_in_a_subfolder():
    cmd = f"git worktree add {DEV}/frankendom-combat-new origin/main"
    assert reason(cmd, cwd=f"{LANE}/scripts/deep").startswith(MESSAGE)


def test_the_message_names_the_lanes_current_side_copy():
    out = reason(f"git worktree add {DEV}/frankendom-combat-new")
    assert ONE[0] in out, out


def test_the_first_side_copy_is_allowed():
    assert reason(f"git worktree add {DEV}/frankendom-combat-new origin/main", existing=[]) == ""


def test_a_long_list_of_copies_is_capped_in_the_message():
    many = [f"{DEV}/frankendom-combat-c{i:02d}" for i in range(36)]
    out = reason(f"git worktree add {DEV}/frankendom-combat-new", existing=many)
    assert out.startswith(MESSAGE) and "c00" in out and "c04" in out and "c05" not in out and "(+31 more)" in out, out


def test_other_lanes_copies_do_not_count():
    others = [f"{DEV}/frankendom-web-a", f"{DEV}/frankendom-web-b", f"{DEV}/frankendom-char-a"]
    assert reason(f"git worktree add {DEV}/frankendom-combat-new", existing=others) == ""
    assert reason(f"git worktree add {DEV}/frankendom-web-new", cwd=f"{DEV}/frankendom-web", existing=others) != ""


def test_the_lane_folder_itself_is_not_a_side_copy():
    assert reason(f"git worktree add {DEV}/frankendom-combat-new", existing=[LANE]) == ""


def test_the_sandboxes_and_main_are_not_lanes_for_this_count():
    many = [f"{DEV}/frankendom-{lane}-{i}" for lane in ("sandbox", "zone-sandbox", "main") for i in range(5)]
    for lane in ("sandbox", "zone-sandbox", "main"):
        cmd = f"git worktree add {DEV}/frankendom-{lane}-new origin/main"
        assert reason(cmd, cwd=f"{DEV}/frankendom-{lane}", existing=many) == "", lane


def test_paths_outside_the_lane_prefix_or_developer_are_not_governed():
    for cmd in (
        f"git worktree add {DEV}/frankendom-web-new origin/main",      # another lane's name, from the combat cwd
        f"git worktree add {DEV}/scratch-combat-new origin/main",
        "git worktree add /tmp/wt origin/main",
        "git worktree add ../elsewhere origin/main",
        f"git worktree add {DEV}/nested/frankendom-combat-new origin/main",
    ):
        assert reason(cmd) == "", cmd


def test_other_git_commands_and_unknown_cwd_are_not_governed():
    for cmd in ("git worktree list", f"git worktree remove {ONE[0]}", "git worktree prune", "git status",
                f"echo git worktree add {DEV}/frankendom-combat-new", f"git commit -m 'git worktree add {DEV}/frankendom-combat-new'"):
        assert reason(cmd) == "", cmd
    assert reason(f"git worktree add {DEV}/frankendom-combat-new", cwd="/tmp") == ""
    assert reason(f"git worktree add {DEV}/frankendom-combat-new", cwd="") == ""
    assert reason(f"git worktree add {DEV}/frankendom-combat-new", cwd=f"{DEV}/not-a-lane") == ""


def test_an_unreadable_worktree_list_never_blocks():
    def boom(prefix):
        raise OSError("git missing")
    assert g.worktree_add_reason(f"git worktree add {DEV}/frankendom-combat-new", LANE, boom) == ""


def test_the_real_git_listing_counts_only_this_lanes_side_copies():
    porcelain = "\n".join(f"worktree {p}\nHEAD abc\nbranch refs/heads/x\n" for p in
                          [f"{DEV}/frankendom-main", LANE, *ONE, f"{DEV}/frankendom-web-a", "/tmp/frankendom-combat-zz"])
    done = mock.Mock(returncode=0, stdout=porcelain)
    with mock.patch.object(g.subprocess, "run", return_value=done):
        assert g.side_copies("frankendom-combat-") == ONE


def test_deny_reaches_the_hook_output_on_the_mac():
    payload = {"tool_name": "Bash", "cwd": LANE,
               "tool_input": {"command": f"git worktree add {DEV}/frankendom-combat-new origin/main"}}
    out = io.StringIO()
    with mock.patch.object(deploy_guard, "_is_mac", return_value=True), \
            mock.patch.object(g, "side_copies", copies(ONE)), \
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
        deploy_guard.main()
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecisionReason"].startswith(MESSAGE)

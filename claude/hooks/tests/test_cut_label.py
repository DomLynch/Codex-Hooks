"""CI work, hooks side (Dom's all-hands rule via Lead and the COO, 2026-10-12, skill ci-runners-first): every release candidate PR carries `ci:release-checks`
at cut, or release-checks runs no rows ("0 CI" on every deploy log from 3 to 12 October). Deploy cuts by hand today (gh pr create --draft --head release-<X>);
the guard refuses that command unless it names the label. Deploy's other cut and pipelined-check commands were never refused and stay allowed
(pre-launch.sh, launch.mjs unit, git merge/push, deploy.sh)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard as g  # noqa: E402

BASE = "gh pr create --draft --base codex/01a09a76/task-1 --head release-CI"
S = "a" * 40


@pytest.mark.parametrize("cmd", [
    f"{BASE} --title t --body b --label ci:release-checks",
    f"{BASE} --title t --label=ci:release-checks",
    f"{BASE} --title t -l ci:release-checks",
    f"{BASE} --title t --label auditor:ready,ci:release-checks",
    f"{BASE} --title t --label 'ci:release-checks'",
    f'{BASE} --title "release-CI: x" --body "b" --label "ci:release-checks"',
    "gh pr create --title t --head feature/x",                                   # not a release candidate
    "gh pr create --title t",
    "gh pr edit 5 --add-label auditor:ready",
    "gh pr view 5",
    "node scripts/cut-release.mjs release-CI 2290@" + S,
    f"bash scripts/pre-launch.sh {S} {S}",
    f"node scripts/vps-shadow/launch.mjs unit {S}",
    "git merge --no-ff -q -m 'Merge pull request #1 (release-CI candidate)' " + S,
    "git push -q origin release-CI",
])
def test_these_are_allowed(cmd):
    assert g.cut_label_reason(cmd) == "", cmd


@pytest.mark.parametrize("cmd", [
    f"{BASE} --title t --body b",
    f"{BASE} --title t --label auditor:ready",
    f"{BASE} --title t --label ci:release-checks-not",                           # a different label
    f"{BASE} --title t --label xci:release-checks",
    "gh pr create --draft --head=release-CI --title t",
    "gh pr create -H release-CI --title t",
    "gh pr create --head DomLynch:release-CI --title t",
    'gh pr create --head "release-CI" --title t',
    "gh pr create --head 'release-CI' --title t",
    "gh 'pr' create --head release-CI --title t",
    f"cd /tmp/x && {BASE} --title t",
    f"git push -q origin release-CI && {BASE} --title t",
    f"{BASE} --title t --body 'ci:release-checks'",                              # the label named in the body is not a label
    f"{BASE} --title t --label auditor:ready && gh pr edit 5 --add-label ci:release-checks",   # label on a later command, not at cut
    f"GH_TOKEN=x {BASE} --title t",
])
def test_a_release_cut_without_the_label_is_refused(cmd):
    assert "ci:release-checks" in g.cut_label_reason(cmd), cmd


def test_main_denies_through_the_hook_protocol(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": f"{BASE} --title t"}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(g, "_is_mac", lambda: False)   # the cut rule is not a Mac-only rule
    g.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ci:release-checks" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_main_allows_a_labelled_cut_and_stays_silent(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": f"{BASE} --title t --label ci:release-checks"}, "cwd": "/tmp"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(g, "_is_mac", lambda: False)
    g.main()
    assert capsys.readouterr().out == ""

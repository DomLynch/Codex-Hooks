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
    "sudo -u x gh pr create --head release-CI --title t --label ci:release-checks",
    'bash -c "gh pr create --head release-CI --title t --label ci:release-checks"',
    'bash -c "gh pr create --head web/x --title t"',
    "sudo -n -- env A=b gh pr create --head release-CI --label ci:release-checks",
    "gh pr create --title t --head feature/x",                                   # not a release candidate
    "gh pr create --title t",
    "gh pr edit 5 --add-label auditor:ready",
    "gh pr view 5",
    "node scripts/cut-release.mjs release-CI 2290@" + S,
    f"bash scripts/pre-launch.sh {S} {S}",
    f"node scripts/vps-shadow/launch.mjs unit {S}",
    "git merge --no-ff -q -m 'Merge pull request #1 (release-CI candidate)' " + S,
    "git push -q origin release-CI",
    'gh pr create --head web/x --body "see --head release-CJ"',                   # --head release- quoted in a body is not the head
    'gh pr create --head web/x --title "cut --head=release-CJ"',
    'gh pr create --body "a; b && c\nd" --head web/x',
    f'{BASE} --body "a; b && c\nd" --label ci:release-checks',                    # separators inside a quoted body do not split the cut
    f"env GH_TOKEN=x {BASE} --label ci:release-checks",
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
    'gh pr create --body "a; b" --head release-CJ',                              # Auditor 2026-10-12: separators inside a quoted body
    'gh pr create --body "a && b" --head release-CJ',
    'gh pr create --body "line one\nline two" --head release-CJ',
    'gh pr create --body "remember --label ci:release-checks" --head release-CJ',   # the label named only in a body or title
    "gh pr create --title 'cut -l ci:release-checks later' --head release-CJ",
    "env GH_TOKEN=x gh pr create --head release-CJ",                             # prefixes
    "/opt/homebrew/bin/gh pr create --head release-CJ",
    "command gh pr create --head release-CJ",
    "env -i gh pr create --head=release-CJ --body 'x; y'",
    "sudo -u x gh pr create --head release-CJ --title t",                          # Auditor LOW on #135: wrapper prefixes with option values
    "sudo -n gh pr create --head release-CJ --title t",
    "sudo -u deploy -H env GH_TOKEN=x gh pr create --head release-CJ",
    "nice -n 5 gh pr create --head release-CJ --title t",
    'bash -c "gh pr create --head release-CJ --title t"',                          # a shell -c script is judged like the command it runs
    "sh -lc 'gh pr create --head release-CJ --title t'",
    'sudo bash -c "git push -q origin release-CJ; gh pr create --head release-CJ --body \'a; b\'"',
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

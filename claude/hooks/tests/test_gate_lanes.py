"""Dom via Strategy, 2026-10-09: the local Stop quality gate runs only in the Deploy lane; the other Frankendom
lanes rely on PR CI (same rows). Sessions that are no Frankendom lane (this hooks lane, the COO) keep the gate."""
from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import stop_orchestrator as so  # noqa: E402


def _run(tmp_path, monkeypatch, folder):
    repo = tmp_path / folder
    repo.mkdir()
    (repo / ".quality-gate.json").write_text("{}")
    called = []
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    monkeypatch.setattr(so, "ci_covered_commit", lambda cwd: ("abc1234", "none"))
    monkeypatch.setattr(so, "unchanged_from_base", lambda cwd: None, raising=False)
    monkeypatch.setattr(so, "active_deploy_lock", lambda: None, raising=False)
    monkeypatch.setattr(so, "gate_busy", lambda cwd: None)
    monkeypatch.setattr(so, "run_project_gate", lambda *a, **k: called.append(1) or {"ok": True})
    assert so.run_aaa_gate(repo, "t") is True
    return bool(called)


def test_non_deploy_frankendom_lanes_skip_the_local_gate(tmp_path, monkeypatch):
    for lane in ("web", "lead", "code-quality", "world", "duels", "combat", "characters"):
        assert _run(tmp_path, monkeypatch, f"frankendom-{lane}") is False, lane


def test_folder_variants_of_a_named_lane_skip(tmp_path, monkeypatch):
    for folder in ("frankendom-combat-cu", "frankendom-char-fix", "frankendom-lead-catalogue", "frankendom-web-evidence"):
        assert _run(tmp_path, monkeypatch, folder) is False, folder


def test_gate_fails_closed_for_deploy_unknown_lane_and_non_lane(tmp_path, monkeypatch):
    for folder in ("frankendom-deploy", "frankendom-newlane", "frankendom-research", "Codex Hooks"):
        assert _run(tmp_path, monkeypatch, folder) is True, folder


# Strategy, 2026-10-09: a session that never edits the repo ran the 420 s gate in the repo it merely ran gh from.
from lib.transcript import session_made_edits  # noqa: E402


def _bash(cmd):
    return [("TOOL CALL", 'Bash: [t1] {"command": "%s"}' % cmd)]


def test_strategy_folder_skips_the_local_gate():
    assert so.gate_skipped_for("strategy")


def test_read_only_sessions_made_no_edits():
    for cmd in ("gh pr view 5 --json state 2>&1", "gh pr merge 5 --merge", "git status --porcelain", "git log -3",
                "node scripts/x.mjs 2>&1 | tail -5", "ls src/fight"):
        assert not session_made_edits(_bash(cmd)), cmd
    assert not session_made_edits([("ASSISTANT", "sed -i would change it")])


def test_edits_and_tree_mutating_bash_are_edits():
    assert session_made_edits([("TOOL CALL", 'Edit: [t1] {"file_path": "/r/src/a.ts", "old_string": "a", "new_string": "b"}')])
    for cmd in ("git commit -m x", "git add -A", "sed -i s/a/b/ src/a.ts", "cat > src/new.ts", "mv a.ts b.ts", "rm src/a.ts"):
        assert session_made_edits(_bash(cmd)), cmd

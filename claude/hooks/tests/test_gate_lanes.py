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


def test_deploy_lane_and_non_lane_sessions_keep_the_gate(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, "frankendom-deploy") is True
    assert _run(tmp_path, monkeypatch, "Codex Hooks") is True

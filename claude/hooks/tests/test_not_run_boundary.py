"""F17 recheck: a not_run quality result is surfaced once at PostTool (edits only) and deferred at Stop."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import post_tool as pt  # noqa: E402
import stop_orchestrator as so  # noqa: E402

NOT_RUN = {"ok": True, "status": "not_run", "results": [], "note": "not checked: a.jsx (.jsx has no checker (no eslint))"}


def _post_tool(monkeypatch, capsys, tmp_path, tool, roots):
    f = tmp_path / "a.jsx"
    f.write_text("x")
    monkeypatch.setattr(pt, "exit_if_audit_child", lambda: None)
    monkeypatch.setattr(pt, "log", lambda *a, **k: None)
    monkeypatch.setattr(pt, "remember_repo", lambda *a, **k: None)
    monkeypatch.setattr(pt, "pending_quality", lambda *a, **k: [])
    monkeypatch.setattr(pt, "update_checkpoint_state", lambda *a, **k: None)
    monkeypatch.setattr(pt, "maybe_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(pt, "resolve_git_cwd", lambda cwd, files: cwd)
    monkeypatch.setattr(pt, "is_git_repo", lambda cwd: True)
    monkeypatch.setattr(pt, "changed_files", lambda cwd: [f])
    monkeypatch.setattr(pt, "changed_files_in_payload", lambda payload: [tmp_path / r / "a.jsx" for r in roots])
    monkeypatch.setattr(pt, "git_root", lambda p: p)
    monkeypatch.setattr(pt, "run_quality_checks", lambda *a, **k: NOT_RUN)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": tool, "session_id": "s", "cwd": str(tmp_path)})))
    pt.main()
    return capsys.readouterr().out


def test_post_tool_edit_emits_one_json_with_notes_from_all_roots(tmp_path, monkeypatch, capsys):
    out = _post_tool(monkeypatch, capsys, tmp_path, "Edit", ["r1", "r2"])
    parsed = json.loads(out)   # two objects would raise 'Extra data'
    assert "decision" not in parsed
    assert parsed["hookSpecificOutput"]["additionalContext"].count("a.jsx") == 2


def test_post_tool_read_never_emits_the_note(tmp_path, monkeypatch, capsys):
    assert _post_tool(monkeypatch, capsys, tmp_path, "Read", []) == ""


def test_stop_gate_not_run_is_deferred_and_user_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    monkeypatch.setattr(so, "milestone_detected", lambda t: True)
    monkeypatch.setattr(so, "safe_run", lambda *a, **k: (0, "a.jsx\n", ""))
    monkeypatch.setattr(so, "run_quality_checks", lambda *a, **k: NOT_RUN)
    monkeypatch.setattr(so, "run_project_aaa_checks", lambda *a, **k: [])
    so._SYSTEM_MESSAGES.clear()
    assert so.run_aaa_gate(tmp_path, "t") == so.DEFERRED
    assert "a.jsx" in so._SYSTEM_MESSAGES[0]
    so._SYSTEM_MESSAGES.clear()
    monkeypatch.setattr(so, "run_quality_checks", lambda *a, **k: {"ok": True, "status": "passed", "results": []})
    assert so.run_aaa_gate(tmp_path, "t") is True
    so._SYSTEM_MESSAGES.clear()


def test_owed_not_run_repo_gets_the_note_again_not_the_missing_contract_block(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(so, "DEFERRAL_NOTICE_PATH", tmp_path / "notified.json")
    for name in ("exit_if_audit_child", "remember_repo"):
        monkeypatch.setattr(so, name, lambda *a, **k: None)
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    pend = []   # Stop 1: nothing pending yet; the cwd gate defers and re-owes the repo, as pending_quality does
    monkeypatch.setattr(so, "pending_quality", lambda s, cwd=None, **k: pend.append(str(cwd)) if cwd else list(pend))
    monkeypatch.setattr(so, "is_git_repo", lambda c: True)
    monkeypatch.setattr(so, "milestone_detected", lambda t: True)
    monkeypatch.setattr(so, "safe_run", lambda *a, **k: (0, "a.jsx\n", ""))
    monkeypatch.setattr(so, "run_quality_checks", lambda *a, **k: NOT_RUN)
    monkeypatch.setattr(so, "run_project_aaa_checks", lambda *a, **k: [])
    monkeypatch.setattr(so, "audit_with_claude", lambda *a, **k: True)
    monkeypatch.setattr(so, "wait_for_mimo_pass", lambda *a, **k: True)
    for _ in range(2):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})))
        so.main()
        out = json.loads(capsys.readouterr().out)
        assert "Missing" not in json.dumps(out)
        assert "a.jsx" in out["systemMessage"]
    assert pend == [str(tmp_path.resolve())]

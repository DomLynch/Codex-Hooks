import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # runnable alone from the repo root (CI runs one file per job)

from lib import rehome  # noqa: E402


def _transcript(tmp_path, last_tool):
    p = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "move me"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": last_tool, "input": {"path": "/x"}}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}),
    ]
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_last_tool_use_detection(tmp_path):
    assert rehome.is_rehome_turn({"transcript_path": _transcript(tmp_path, rehome.REHOME_TOOL)}).startswith("last tool_use")
    assert rehome.is_rehome_turn({"transcript_path": _transcript(tmp_path, "Bash")}) == ""
    assert rehome.is_rehome_turn({"transcript_path": str(tmp_path / "missing.jsonl")}) == ""
    assert rehome.is_rehome_turn({}) == ""


def test_sentinel_is_consumed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SCRATCHPAD", str(tmp_path))
    sentinel = tmp_path / rehome.SENTINEL
    sentinel.write_text("")
    assert rehome.is_rehome_turn({"transcript_path": _transcript(tmp_path, "Bash")}).startswith("sentinel")
    assert not sentinel.exists()
    assert rehome.is_rehome_turn({"transcript_path": _transcript(tmp_path, "Bash")}) == ""


def test_mimo_audit_fast_exits_before_any_model_call(tmp_path, monkeypatch):
    import mimo_audit as m
    monkeypatch.setattr(m, "exit_if_audit_child", lambda: None)
    monkeypatch.setattr(m, "call_mimo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    monkeypatch.setattr(m, "normalize_payload", lambda p: (_ for _ in ()).throw(AssertionError("transcript parsed")))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "transcript_path": _transcript(tmp_path, rehome.REHOME_TOOL)})))
    t0 = time.monotonic()
    try:
        m.main()
    except SystemExit as e:
        assert e.code == 0
    assert time.monotonic() - t0 < 1.0


def test_stop_orchestrator_fast_exits_before_gate(tmp_path, monkeypatch):
    import stop_orchestrator as so
    monkeypatch.setattr(so, "exit_if_audit_child", lambda: None)
    monkeypatch.setattr(so, "normalize_payload", lambda p: (_ for _ in ()).throw(AssertionError("transcript parsed")))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "transcript_path": _transcript(tmp_path, rehome.REHOME_TOOL)})))
    t0 = time.monotonic()
    so.main()
    assert time.monotonic() - t0 < 1.0


def test_ordinary_turn_is_not_fast_exited(tmp_path, monkeypatch):
    import mimo_audit as m
    monkeypatch.setattr(m, "exit_if_audit_child", lambda: None)
    seen = {}

    def normalize(p):
        seen["called"] = True
        raise SystemExit(0)
    monkeypatch.setattr(m, "normalize_payload", normalize)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "transcript_path": _transcript(tmp_path, "Bash")})))
    try:
        m.main()
    except SystemExit:
        pass
    assert seen.get("called")

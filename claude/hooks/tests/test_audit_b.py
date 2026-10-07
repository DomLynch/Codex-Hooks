"""Audit B (Stop path): one JSON out, rehome per turn, exact verdicts, visible unresolved note, reviewer cap,
one absolute deadline, change evidence before the no-work shortcut, approval freshness."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import mimo_audit  # noqa: E402
import stop_orchestrator as so  # noqa: E402
from lib import audit_state, ledger, rehome  # noqa: E402
from lib.claude_adapter import synthesize_turn_id  # noqa: E402
from lib.verdicts import normalize_verdict, valid_verdict  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_output_state():
    so._EMITTED = False
    so._CONTEXT_NOTES.clear()
    so._SYSTEM_MESSAGES.clear()
    yield


def _jl(path: Path, entries: list[dict]) -> str:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(path)


def _user(text, ts="2026-10-07T10:00:00Z", **kw):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **kw}


def _tool(name, **inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": inp}]}}


def _result(text="ok"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def _say(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


# --- F03 -------------------------------------------------------------------
def test_f03_deferral_and_block_serialize_as_one_json(tmp_path, monkeypatch, capsys):
    deferred, missing = tmp_path / "a", tmp_path / "b"
    deferred.mkdir()
    missing.mkdir()
    (deferred / ".quality-gate.json").write_text("{}")
    monkeypatch.setattr(so, "DEFERRAL_NOTICE_PATH", tmp_path / "notified.json")
    monkeypatch.setattr(so, "exit_if_audit_child", lambda: None)
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    monkeypatch.setattr(so, "remember_repo", lambda *a, **k: None)
    monkeypatch.setattr(so, "pending_quality", lambda *a, **k: [str(deferred), str(missing)])
    monkeypatch.setattr(so, "run_aaa_gate",
                        lambda cwd, *a, **k: so.defer_gate({"reason": "deploy in flight"}, k.get("session_id", "s")))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})))
    with pytest.raises(SystemExit):
        so.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)  # 'Extra data' on two objects
    assert parsed["decision"] == "block"
    assert "Missing" in parsed["reason"]


def test_f03_deferral_alone_is_one_json(tmp_path, monkeypatch, capsys):
    deferred = tmp_path / "a"
    deferred.mkdir()
    (deferred / ".quality-gate.json").write_text("{}")
    monkeypatch.setattr(so, "DEFERRAL_NOTICE_PATH", tmp_path / "notified.json")
    monkeypatch.setattr(so, "exit_if_audit_child", lambda: None)
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    monkeypatch.setattr(so, "remember_repo", lambda *a, **k: None)
    monkeypatch.setattr(so, "pending_quality", lambda *a, **k: [str(deferred)])
    monkeypatch.setattr(so, "run_aaa_gate",
                        lambda cwd, *a, **k: so.defer_gate({"reason": "deploy in flight"}, k.get("session_id", "s")))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})))
    so.main()
    parsed = json.loads(capsys.readouterr().out)
    assert "deferred" in parsed["hookSpecificOutput"]["additionalContext"]


# --- F05 -------------------------------------------------------------------
def test_f05_earlier_turn_rehome_does_not_exempt_later_turn(tmp_path):
    path = _jl(tmp_path / "t.jsonl", [
        _user("move me", "2026-10-07T10:00:00Z"),
        _tool(rehome.REHOME_TOOL, path="/x"), _result(), _say("moved"),
        _user("implement X and run tests", "2026-10-07T10:05:00Z"),
        _say("implemented, tests pass"),
    ])
    assert rehome.is_rehome_turn({"transcript_path": path}) == ""


def test_f05_same_turn_rehome_still_exempt(tmp_path):
    path = _jl(tmp_path / "t.jsonl", [
        _user("move me"), _tool(rehome.REHOME_TOOL, path="/x"), _result(), _say("moved"),
    ])
    assert rehome.is_rehome_turn({"transcript_path": path}).startswith("last tool_use")


# --- F12 -------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "VERDICT: PASSIVE REVIEW ONLY - I have not reviewed the code.",
    "VERDICT: ISSUESX\n1. a finding",
    "VERDICT: PASSED",
])
def test_f12_prefix_verdicts_are_not_accepted(text):
    assert not valid_verdict(text)
    assert not valid_verdict(normalize_verdict(text))


def test_f12_exact_verdicts_still_accepted():
    assert valid_verdict(normalize_verdict("VERDICT: PASS"))
    assert valid_verdict(normalize_verdict("PASS."))
    assert valid_verdict(normalize_verdict("VERDICT: ISSUES\n1. real finding"))


# --- F13 -------------------------------------------------------------------
def test_f13_unresolved_release_is_a_visible_system_message(monkeypatch, capsys):
    monkeypatch.setattr(mimo_audit, "log", lambda *a, **k: None)
    mimo_audit._HANDOFF.clear()
    with pytest.raises(SystemExit) as exc:
        mimo_audit.allow_stop("max iterations", unresolved=True)
    assert exc.value.code == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "unresolved" in msg.lower()
    assert "decision" not in msg


def test_f13_plain_allow_stays_silent(monkeypatch, capsys):
    monkeypatch.setattr(mimo_audit, "log", lambda *a, **k: None)
    mimo_audit._HANDOFF.clear()
    with pytest.raises(SystemExit):
        mimo_audit.allow_stop("audit passed")
    assert capsys.readouterr().out == ""


# --- F10 -------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.start = self.now

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_f10_one_absolute_deadline_for_retries_empty_and_repair(tmp_path, monkeypatch, capsys):
    clock = FakeClock()
    fake_time = SimpleNamespace(monotonic=clock.monotonic, time=clock.time, sleep=clock.sleep, strftime=time.strftime)
    calls = []

    def fake_call(prompt, max_tokens=0, timeout=100, lane="", system=""):
        calls.append(timeout)
        clock.now += timeout          # the call burns its whole timeout
        n = len(calls)
        if n in (1, 3):
            raise TimeoutError("slow")
        return "" if n == 2 else "garbage, no verdict"

    monkeypatch.setattr(mimo_audit, "time", fake_time)
    monkeypatch.setattr(mimo_audit, "call_mimo", fake_call)
    monkeypatch.setattr(mimo_audit, "exit_if_audit_child", lambda: None)
    monkeypatch.setattr(mimo_audit, "log", lambda *a, **k: None)
    monkeypatch.setattr(mimo_audit, "report_failure", lambda *a, **k: None)
    monkeypatch.setattr(mimo_audit, "latest_task_slice_claude", lambda *a, **k: "x" * 600 + " TOOL CALL edit implemented")
    monkeypatch.setattr(audit_state, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(audit_state, "VERDICT_FILE", tmp_path / "v.json")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "f10", "turn_id": "t", "cwd": str(tmp_path)})))
    with pytest.raises(SystemExit):
        mimo_audit.main()
    elapsed = clock.now - clock.start
    assert elapsed <= mimo_audit.AUDIT_HOOK_TIMEOUT_S - 10, f"{elapsed}s of a {mimo_audit.AUDIT_HOOK_TIMEOUT_S}s hook"
    assert capsys.readouterr().out  # a final block/allow still got written


# --- F08 -------------------------------------------------------------------
def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


def test_f08_bash_edit_with_terse_answer_is_reviewed_with_the_diff(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "calc.py").write_text("def f(a, b):\n    return a + b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    (repo / "calc.py").write_text("def f(a, b):\n    return a - b\n")   # what the Bash sed did
    path = _jl(tmp_path / "t.jsonl", [
        _user("change add to subtract in calc.py"),
        _tool("Bash", command="sed -i '' 's/a + b/a - b/' calc.py"), _result(""), _say("Updated."),
    ])
    prompts = []
    monkeypatch.setattr(mimo_audit, "call_mimo", lambda prompt, **k: prompts.append(prompt) or "VERDICT: PASS")
    monkeypatch.setattr(mimo_audit, "exit_if_audit_child", lambda: None)
    monkeypatch.setattr(mimo_audit, "log", lambda *a, **k: None)
    monkeypatch.setattr(mimo_audit, "record_lesson", lambda *a, **k: None)
    monkeypatch.setattr(audit_state, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(audit_state, "VERDICT_FILE", tmp_path / "v.json")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "f08", "turn_id": "t", "cwd": str(repo), "transcript_path": path, "last_assistant_message": "Updated."})))
    with pytest.raises(SystemExit):
        mimo_audit.main()
    assert len(prompts) == 1, "a code-changing Bash turn must reach the model"
    assert "return a - b" in prompts[0]


# --- F04 -------------------------------------------------------------------
def test_f04_turn_id_ignores_appended_metadata_but_not_a_new_user_turn(tmp_path):
    p = tmp_path / "t.jsonl"
    base = [_user("do it", uuid="u1"), _say("done")]
    _jl(p, base)
    first = synthesize_turn_id("s1", str(p), False)
    time.sleep(1.1)
    _jl(p, base + [{"type": "last-prompt", "lastPrompt": "do it"}, {"type": "system", "content": "meta"}])
    assert synthesize_turn_id("s1", str(p), False) == first
    _jl(p, base + [_user("another thing", "2026-10-07T11:00:00Z", uuid="u2"), _say("ok")])
    assert synthesize_turn_id("s1", str(p), False) != first


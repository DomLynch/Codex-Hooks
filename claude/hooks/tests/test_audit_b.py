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
    monkeypatch.setattr(so, "audit_with_claude", lambda *a, **k: True)
    monkeypatch.setattr(so, "wait_for_mimo_pass", lambda *a, **k: True)
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
    assert so.parse_verdict(text)[0] != "PASS"


def test_f12_exact_verdicts_still_accepted():
    assert valid_verdict(normalize_verdict("VERDICT: PASS"))
    assert valid_verdict(normalize_verdict("PASS."))
    assert valid_verdict(normalize_verdict("VERDICT: ISSUES\n1. real finding"))
    assert so.parse_verdict("VERDICT: PASS\nfine")[0] == "PASS"
    assert so.parse_verdict("VERDICT: ISSUES\n1. x")[0] == "ISSUES"
    assert so.parse_verdict("")[0] == "ERROR"


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


# --- F14 -------------------------------------------------------------------
def test_f14_secondary_reviewer_cap_stops_blocking(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(so, "CLAUDE_ITERATIONS_PATH", tmp_path / "iters.json")
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    monkeypatch.setattr(so, "CLAUDE_AUDIT_ENABLED", True)
    monkeypatch.setattr(so, "call_claude", lambda *a, **k: "VERDICT: ISSUES\n1. still wrong")
    results, notes = [], []
    for _ in range(5):
        so._SYSTEM_MESSAGES.clear()
        so._EMITTED = False   # each iteration stands for a separate hook process
        try:
            results.append(so.audit_with_claude("s1", "x" * 500, ""))
        except SystemExit:
            results.append("blocked")
        notes.append(list(so._SYSTEM_MESSAGES))
    out = capsys.readouterr().out
    assert results[:2] == ["blocked", "blocked"]
    assert results[2] not in ("blocked", True), f"cap must stop blocking without approving: {results}"
    assert notes[2], "terminal state needs a visible note"
    assert out.count('"decision"') == 4   # blocks 1-2, terminal release, then the counter restarts


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
def _git(repo, *args, date=None):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _review_bash_turn(tmp_path, monkeypatch, repo, user_ts, command, answer="Updated."):
    """Run mimo_audit.main for a turn whose only tool call is Bash; return the prompts the reviewer received."""
    path = _jl(tmp_path / "t.jsonl", [
        _user("change the code", ts=user_ts), _tool("Bash", command=command), _result(""), _say(answer),
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
        {"session_id": "f08", "turn_id": "t", "cwd": str(repo), "transcript_path": path, "last_assistant_message": answer})))
    with pytest.raises(SystemExit):
        mimo_audit.main()
    return prompts


def _calc_repo(tmp_path, body="def f(a, b):\n    return a + b\n", date="2000-01-01T00:00:00Z"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "calc.py").write_text(body)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init", date=date)
    return repo


# Rule: before any skip decision and for the reviewer's evidence, the turn's change is ONE snapshot: the turn's
# commits, plus everything now different from HEAD, plus untracked files created since the turn began. Fixed dates
# keep these independent of the wall clock.
USER_TS = "2020-01-01T00:00:00Z"


def test_f08_bash_edit_with_terse_answer_is_reviewed_with_the_diff(tmp_path, monkeypatch):
    repo = _calc_repo(tmp_path)
    (repo / "calc.py").write_text("def f(a, b):\n    return a - b\n")   # what the Bash sed did
    prompts = _review_bash_turn(tmp_path, monkeypatch, repo, USER_TS, "sed -i '' 's/a + b/a - b/' calc.py")
    assert len(prompts) == 1, "a code-changing Bash turn must reach the model"
    assert "return a - b" in prompts[0]


def test_f08_bash_created_untracked_file_is_reviewed(tmp_path, monkeypatch):
    repo = _calc_repo(tmp_path)
    (repo / "helper.py").write_text("def helper():\n    return 'brand new'\n")   # created by a Bash heredoc
    prompts = _review_bash_turn(tmp_path, monkeypatch, repo, USER_TS, "cat > helper.py <<EOF ... EOF")
    assert len(prompts) == 1, "an untracked file created by Bash is a code change: no SKIP_CONTENTLESS"
    assert "brand new" in prompts[0]


def test_f08_dirty_edit_after_an_in_turn_commit_is_in_the_diff(tmp_path, monkeypatch):
    repo = _calc_repo(tmp_path)
    (repo / "calc.py").write_text("def f(a, b):\n    return a * b\n")
    _git(repo, "commit", "-aqm", "multiply", date="2020-01-01T00:00:01Z")
    (repo / "calc.py").write_text("def f(a, b):\n    return a - b\n")   # Bash edit after the commit, uncommitted
    prompts = _review_bash_turn(tmp_path, monkeypatch, repo, USER_TS, "git commit -am multiply; sed -i '' 's/a \\* b/a - b/' calc.py")
    assert len(prompts) == 1
    assert "return a * b" in prompts[0] and "return a - b" in prompts[0]


def test_f08_a_file_untracked_before_the_turn_is_not_change_evidence(tmp_path, monkeypatch):
    repo = _calc_repo(tmp_path)
    (repo / "old_notes.txt").write_text("left over from last week")
    import os
    os.utime(repo / "old_notes.txt", (1_000_000_000, 1_000_000_000))   # long before USER_TS
    prompts = _review_bash_turn(tmp_path, monkeypatch, repo, USER_TS, "ls")
    assert prompts == [], "a stale untracked file must not turn a no-op turn into a model call"


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


def test_f04_stale_pass_from_an_earlier_attempt_does_not_approve(tmp_path, monkeypatch):
    verdicts = tmp_path / "v.json"
    old = time.time() - 3600
    verdicts.write_text(json.dumps({"s1": {"turn_id": "T", "verdict": "PASS", "updated_at": old, "started_at": old}}))
    monkeypatch.setattr(so, "MIMO_VERDICT_PATH", verdicts)
    monkeypatch.setattr(so, "MIMO_WAIT_TIMEOUT_S", 0.3)
    real_sleep = time.sleep
    monkeypatch.setattr(so.time, "sleep", lambda s: real_sleep(0.05))
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    assert so.wait_for_mimo_pass("s1", "T") is False


def test_f04_current_attempt_pass_approves(tmp_path, monkeypatch):
    verdicts = tmp_path / "v.json"
    monkeypatch.setattr(audit_state, "VERDICT_FILE", verdicts)
    audit_state.save_verdict("s1", "T", "PASS", "VERDICT: PASS")
    monkeypatch.setattr(so, "MIMO_VERDICT_PATH", verdicts)
    monkeypatch.setattr(so, "log", lambda *a, **k: None)
    assert so.wait_for_mimo_pass("s1", "T") is True

"""Regressions for quality routing, finding retention and deadline cleanup."""
import io
import json
import os
import random
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import post_tool
from lib import git, ledger, quality


def test_wrapped_patch_registers_repo_and_readonly_does_not_check(tmp_path):
    repo = tmp_path / 'project'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    target = repo / 'new.py'
    target.write_text('def broken(:\n')
    patch = f'*** Begin Patch\n*** Add File: {target}\n+def broken(:\n*** End Patch'
    wrapped = f'text(await tools.apply_patch({json.dumps(patch)}));'
    single_quoted = 'tools.apply_patch(' + repr(patch) + ')'
    template = 'tools.apply_patch(`' + patch + '`)'
    for tool_input in (patch, wrapped, single_quoted, template, {'code': wrapped}, {'input': patch}, {'patch': patch}):
        payload = dict(cwd=str(tmp_path), session_id='wrapped', tool_name='exec', tool_input=tool_input)
        assert quality.changed_files_in_payload(payload) == [target]
        with mock.patch.object(quality, 'PENDING_PATH', tmp_path/'pending.json'), \
             mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
             mock.patch.object(post_tool, 'remember_repo'), mock.patch.object(post_tool, 'sync_codegraph'), \
             mock.patch.object(post_tool, 'log'), \
             mock.patch.object(post_tool, 'run_quality_checks', return_value={'ok': False, 'results': []}) as checks, \
             redirect_stdout(io.StringIO()) as out:
            try:
                post_tool.main()
            except SystemExit:
                pass
            assert checks.call_args.args[:2] == ([target], repo.resolve())
            assert quality.pending_quality('wrapped') == [str(repo.resolve())]
            assert json.loads(out.getvalue())['decision'] == 'block'
    payload = dict(cwd=str(repo), tool_name='exec', tool_input={'cmd':'git status --short'})
    with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
         mock.patch.object(post_tool, 'run_quality_checks') as checks, \
         mock.patch.object(post_tool, 'pending_quality') as pending, \
         mock.patch.object(post_tool, 'remember_repo'):
        post_tool.main()
        checks.assert_not_called()
        pending.assert_not_called()


def test_edit_fallback_registers_completion_gate(tmp_path):
    subprocess.run(['git', 'init', '-q', tmp_path], check=True)
    payload = dict(cwd=str(tmp_path), session_id='fallback', tool_name='Write', tool_input={})
    with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
         mock.patch.object(post_tool, 'resolve_git_cwd', return_value=tmp_path), \
         mock.patch.object(post_tool, 'is_git_repo', return_value=True), \
         mock.patch.object(post_tool, 'changed_files', return_value=[tmp_path/'new.py']), \
         mock.patch.object(post_tool, 'run_quality_checks', return_value={'ok':True}), \
         mock.patch.object(post_tool, 'remember_repo'), mock.patch.object(post_tool, 'sync_codegraph'), \
         mock.patch.object(post_tool, 'log'), mock.patch.object(quality, 'PENDING_PATH', tmp_path/'pending.json'):
        post_tool.main()
        assert quality.pending_quality('fallback') == [str(tmp_path)]


def test_open_findings_survive_capacity_and_sorted_storage(tmp_path):
    rng = random.Random(73)
    sessions = [f'{i:03}-session' for i in range(65)]
    rng.shuffle(sessions)
    with mock.patch.object(ledger, 'LEDGER_PATH', tmp_path/'ledger.json'):
        for sid in sessions:
            ledger.register_findings(sid, 'VERDICT: ISSUES\n1. Fix real defect.')
        for sid in sessions:
            assert len(ledger.open_findings(sid)) == 1
        for sid in sessions[:50]:
            ledger.apply_decisions(sid, '[F1] fixed: verified fix')
        saved=json.loads(ledger.LEDGER_PATH.read_text())
        assert len(saved) <= 40 + 15
        for sid in sessions[50:]:
            assert len(ledger.open_findings(sid)) == 1


def test_project_deadline_has_bounded_drain_for_escaped_child(tmp_path):
    pidfile=tmp_path/'escaped.pid'
    child='import time; time.sleep(3)'
    parent=('import subprocess,sys,time; from pathlib import Path; '
            f'p=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
            f'Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(10)')
    (tmp_path/'.quality-gate.json').write_text(json.dumps({'commands':[[sys.executable,'-c',parent]]}))
    started=time.monotonic()
    try:
        with mock.patch.object(git,'KILL_DRAIN_S',0.1):
            result=quality.run_project_gate(tmp_path,timeout=0.3)
        assert not result['ok']
        assert time.monotonic()-started < 2
        assert 'timed out' in result['results'][0]['output']
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()),9)
            except ProcessLookupError:
                pass


def test_loose_files_never_queue_workspace_gate_and_mixed_edits_all_get_checked(tmp_path):
    workspace = tmp_path / 'Business'
    loose = workspace / 'artifacts' / 'character.py'
    loose.parent.mkdir(parents=True)
    loose.write_text('value = 1\n')
    repo = workspace / 'game'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', repo], check=True)
    source = repo / 'game.py'
    source.write_text('value = 1\n')
    (workspace / '.quality-gate.json').write_text(json.dumps({'commands': [
        [sys.executable, '-c', 'raise SystemExit("UNRELATED GAME")']]}))
    rng = random.Random(87)
    cases = [[loose], [source], [source, loose]]
    cases += [rng.sample([source, loose], 2) for _ in range(8)]
    with mock.patch.object(quality, 'PENDING_PATH', tmp_path / 'pending.json'), \
         mock.patch.object(post_tool, 'remember_repo'), mock.patch.object(post_tool, 'log'), \
         mock.patch.object(post_tool, 'sync_codegraph'):
        for i, files in enumerate(cases):
            sid = f'case-{i}'
            patch = '*** Begin Patch\n' + ''.join(f'*** Update File: {p}\n+x\n' for p in files) + '*** End Patch'
            payload = dict(cwd=str(workspace), session_id=sid, tool_name='apply_patch', tool_input=patch)
            with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                 mock.patch.object(post_tool, 'run_quality_checks', return_value={'ok': True}) as checks:
                post_tool.main()
            assert quality.pending_quality(sid) == ([str(repo.resolve())] if source in files else [])
            checked = {p: call.args[1] for call in checks.call_args_list for p in call.args[0]}
            assert checked == {p: repo.resolve() if p == source else loose.parent.resolve() for p in files}


def test_stop_rejects_workspace_assignment_but_enforces_actual_repositories(tmp_path):
    import stop_orchestrator

    workspace = tmp_path / 'Business'
    workspace.mkdir()
    repo = workspace / 'game'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', repo], check=True)
    marker = tmp_path / 'unrelated-ran'
    (workspace / '.quality-gate.json').write_text(json.dumps({'commands': [[sys.executable, '-c',
        f'from pathlib import Path; Path({str(marker)!r}).touch(); raise SystemExit(7)']]}))
    contract = repo / '.quality-gate.json'
    contract.write_text(json.dumps({'commands': [[sys.executable, '-c', 'raise SystemExit(3)']]}))
    home = tmp_path / 'home'
    state = home / '.codex/state/pending_quality.json'
    state.parent.mkdir(parents=True)
    env = {**os.environ, 'HOME': str(home), 'HOOK_SENTRY_DSN': ''}
    payload = dict(cwd=str(workspace), session_id='isolated', last_assistant_message='Character work checked.')

    def run(pending, **overrides):
        state.write_text(json.dumps({'isolated': list(map(str, pending))}))
        result = subprocess.run([sys.executable, stop_orchestrator.__file__],
                                input=json.dumps({**payload, **overrides}), env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert run([workspace]) == ''
    assert not marker.exists()
    assert json.loads(state.read_text()) == {}
    for pending in ([workspace, repo], [repo, workspace], [repo]):
        assert json.loads(run(pending))['decision'] == 'block'
        assert str(repo) in json.loads(state.read_text())['isolated']
        assert not marker.exists()
    contract.unlink()
    assert 'Missing' in run([repo])
    contract.write_text(json.dumps({'commands': [[sys.executable, '-c', 'pass']]}))
    assert run([workspace, repo]) == ''
    assert json.loads(state.read_text()) == {}
    contract.write_text(json.dumps({'commands': [[sys.executable, '-c', 'raise SystemExit(3)']]}))
    # A named sibling in prose cannot select a project's gate, even on a milestone.
    assert run([], last_assistant_message='Ready to deploy now. Reference ' + json.dumps({'workdir': str(repo)})) == ''
    # A real repo launch still checks its own milestone contract without pending edits.
    assert json.loads(run([], cwd=str(repo), last_assistant_message='Ready to deploy now.'))['decision'] == 'block'

    other = workspace / 'other-game'
    other.mkdir()
    subprocess.run(['git', 'init', '-q', other], check=True)
    (other / '.quality-gate.json').write_text(json.dumps({'commands': [[sys.executable, '-c', 'pass']]}))
    assert run([other], cwd=str(repo), last_assistant_message='Ready to deploy now.') == ''

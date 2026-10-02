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

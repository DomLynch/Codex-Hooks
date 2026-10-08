"""Regressions for completed user turns and lean installation."""
import json
import os
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.transcript import latest_task_slice


def message(role, text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role, 'content': text}}


def incoming(text):
    return {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
        'type': 'FunctionCallOutput', 'namespace': 'codex_app', 'name': 'send_message_to_thread',
        'output': '<codex_delegation><input>' + text + '</input></codex_delegation>'}}}


def test_original_user_outcome_survives_many_agent_updates(tmp_path):
    rng = random.Random(73)
    for outcome in ('Installed all requested skills; verified enabled.', 'FAILED installation; requested skills remain missing.'):
        for _ in range(12):
            rows = [{'type': 'turn_context', 'payload': {'turn_id': 'user-turn'}},
                    message('user', 'Install these skills globally; do not deploy anything.')]
            rows += [message('assistant', 'Checking prerequisites.')]
            rows += [{'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'Environment inspected ' * 100}}]
            rows += [incoming('Unrelated build progress'), message('assistant', outcome),
                     {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'user-turn', 'last_agent_message': outcome}}]
            for i in range(rng.randint(2, 8)):
                rows += [{'type': 'turn_context', 'payload': {'turn_id': f'agent-{i}'}}, incoming('Game build progress ' * rng.randint(20, 100)),
                         {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'Build inspection ' * 100}},
                         message('assistant', 'Current game work is still under review.')]
            path = tmp_path / 'transcript.jsonl'
            path.write_text('\n'.join(map(json.dumps, rows)))
            for preserve in (False, True):
                text = latest_task_slice(str(path), preserve_request=preserve)
                assert outcome in text
                assert 'not user authority' in text
                assert 'Current game work' in text
                assert len(text) <= 4000
            rows += [{'type': 'turn_context', 'payload': {'turn_id': 'new-user'}}, message('user', 'Stop; report only.')]
            path.write_text('\n'.join(map(json.dumps, rows)))
            text = latest_task_slice(str(path), preserve_request=True)
            assert 'Stop; report only.' in text
            assert outcome not in text


def test_install_rejects_untracked_runtime_files_without_writes(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'hooks').mkdir()
    source = Path(__file__).resolve().parents[2] / 'scripts/install.sh'
    (repo / 'scripts/install.sh').write_bytes(source.read_bytes())
    subprocess.run(['git', 'init', '-q', repo], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    (repo / 'hooks/old 2.py').write_text('stale = True\n')
    target = tmp_path / 'install'
    result = subprocess.run(['bash', str(repo / 'scripts/install.sh')], env={**os.environ, 'CODEX_HOME': str(target)}, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'untracked' in result.stderr.lower()
    assert not target.exists()


def test_wrong_turn_completion_cannot_replace_user_outcome(tmp_path):
    rows = [{'type': 'turn_context', 'payload': {'turn_id': 'real'}},
            message('user', 'Install the skills; do not deploy.'),
            message('assistant', 'Installation still failing.'),
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'unrelated', 'last_agent_message': 'FAKE COMPLETION'}},
            incoming('Game update ' * 100), message('assistant', 'Game progress only.')]
    path = tmp_path / 'turn.jsonl'
    path.write_text('\n'.join(map(json.dumps, rows)))
    text = latest_task_slice(str(path), preserve_request=True)
    assert 'FAKE COMPLETION' not in text
    assert 'Installation still failing' in text
    assert 'not user authority' in text


def test_many_edits_have_quality_gates_without_model_calls(tmp_path):
    import io
    from unittest import mock
    import post_tool
    from lib import quality

    repo = tmp_path / 'project'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', repo], check=True)
    target = repo / 'module.py'
    target.write_text('value = 1\n')
    payload = {'session_id': 'edits', 'cwd': str(repo), 'tool_name': 'Write', 'tool_input': {'path': str(target)},
               'last_assistant_message': 'Current task has substantive work and checks. ' * 40}
    with mock.patch.object(quality, 'PENDING_PATH', tmp_path / 'pending.json'), \
         mock.patch.object(post_tool, 'MIMO_CHECKPOINT_AUDIT_PATH', tmp_path / 'old-counter.json', create=True), \
         mock.patch.object(post_tool, 'call_mimo', return_value='VERDICT: PASS', create=True) as model, \
         mock.patch.object(post_tool, 'run_quality_checks', return_value={'ok': True}) as checks, \
         mock.patch.object(post_tool, 'remember_repo'), mock.patch.object(post_tool, 'log'):
        for _ in range(40):
            with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))):
                post_tool.main()
        assert checks.call_count == 40
        assert quality.pending_quality('edits') == [str(repo.resolve())]
        model.assert_not_called()

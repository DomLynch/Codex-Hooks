import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import quality  # noqa: E402
import stop_orchestrator  # noqa: E402
import post_tool  # noqa: E402


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def contract(self, commands):
        (self.root / '.quality-gate.json').write_text(json.dumps({'commands': commands}))

    def test_checkpoint_does_not_infer_skipped_discovery_from_elision(self):
        self.assertIn('Missing Semble/Codegraph receipts alone', post_tool.MIMO_CHECKPOINT_PROMPT)
        self.assertIn('flag only explicit, current noncompliance', post_tool.MIMO_CHECKPOINT_PROMPT)

    def test_contract_success_and_real_failure(self):
        for code in (0, 1):
            self.contract([[sys.executable, '-c', f'raise SystemExit({code})']])
            self.assertEqual(quality.run_project_gate(self.root)['ok'], code == 0)

    def test_codex_commands_are_scoped_and_keep_completion_checks(self):
        for code in (0, 1):
            with self.subTest(code=code):
                (self.root / '.quality-gate.json').write_text(json.dumps({
                    'commands': [[sys.executable, '-c', 'raise SystemExit(99)']],
                    'codex_commands': [[sys.executable, '-c', 'print("codex only")']],
                    'completion_commands': [[sys.executable, '-c', f'raise SystemExit({code})']],
                }))
                result = quality.run_project_gate(self.root)
                self.assertEqual(result['ok'], code == 0)
                self.assertEqual(len(result['results']), 2)
                self.assertIn('codex only', result['results'][0]['output'])

    def test_invalid_codex_commands_fail_closed(self):
        for commands in (None, [], 'echo pass', [[]]):
            (self.root / '.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']], 'codex_commands': commands,
            }))
            self.assertFalse(quality.run_project_gate(self.root)['ok'])

    def test_standalone_command_lists_execute_all_checks_and_report_failure(self):
        for key in ('codex_commands', 'completion_commands'):
            for code in (0, 7):
                with self.subTest(key=key, code=code):
                    commands = [[sys.executable, '-c',
                                 f'print("check {i}"); raise SystemExit({code if i == 3 else 0})']
                                for i in (1, 2, 3)]
                    (self.root / '.quality-gate.json').write_text(json.dumps({key: commands}))
                    result = quality.run_project_gate(self.root)
                    self.assertEqual(result['ok'], code == 0)
                    self.assertEqual([r['output'].strip() for r in result['results']],
                                     ['check 1', 'check 2', 'check 3'])

    def test_empty_or_malformed_completion_only_contract_fails_without_execution(self):
        for contract in ({}, {'completion_commands': []}, {'completion_commands': None},
                         {'completion_commands': 'echo pass'}, {'completion_commands': [[]]}):
            with self.subTest(contract=contract), patch.object(quality, 'safe_run') as run:
                (self.root / '.quality-gate.json').write_text(json.dumps(contract))
                self.assertFalse(quality.run_project_gate(self.root)['ok'])
                run.assert_not_called()

    def test_completion_checks_cannot_hide_behind_green_unit_checks(self):
        for code in (1, 0):
            (self.root / '.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'print("unit checks pass")']],
                'completion_commands': [[sys.executable, '-c', f'print("workflow receipt"); raise SystemExit({code})']],
            }))
            result = quality.run_project_gate(self.root)
            self.assertEqual(result['ok'], code == 0)
            self.assertEqual(len(result['results']), 2)
            self.assertIn('workflow receipt', result['results'][-1]['output'])

    def test_invalid_completion_checks_fail_closed(self):
        for commands in ('echo pass', [[]], [['missing-workflow-tool']]):
            (self.root / '.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']], 'completion_commands': commands,
            }))
            self.assertFalse(quality.run_project_gate(self.root)['ok'])

    def test_missing_invalid_empty_and_missing_executable_block(self):
        self.assertFalse(quality.run_project_gate(self.root)['ok'])
        for commands in ([], 'echo pass', [[]], [['definitely-missing-quality-tool']]):
            self.contract(commands)
            self.assertFalse(quality.run_project_gate(self.root)['ok'])

    def test_deadline_blocks(self):
        self.contract([[sys.executable, '-c', 'import time; time.sleep(5)']])
        self.assertFalse(quality.run_project_gate(self.root, timeout=0.05)['ok'])

    def test_project_timeout_is_bounded_and_overridable(self):
        self.contract([[sys.executable, '-c', 'pass']])
        with patch.object(quality, 'safe_run', return_value=(0, '', '')) as runner:
            self.assertTrue(quality.run_project_gate(self.root, timeout=500)['ok'])
            self.assertGreater(runner.call_args.kwargs['timeout'], 410)
            self.assertLessEqual(runner.call_args.kwargs['timeout'], 420)

            self.root.joinpath('.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']], 'timeout_seconds': 30,
            }))
            self.assertTrue(quality.run_project_gate(self.root, timeout=500)['ok'])
            self.assertLessEqual(runner.call_args.kwargs['timeout'], 30)

    def test_invalid_project_timeout_fails_closed(self):
        for value in (0, 421, True, 1.5, '420'):
            self.root.joinpath('.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']], 'timeout_seconds': value,
            }))
            with patch.object(quality, 'safe_run') as popen:
                self.assertFalse(quality.run_project_gate(self.root)['ok'])
                popen.assert_not_called()

    def test_missing_tools_are_not_success(self):
        file = self.root/'valid.py'
        file.write_text('x = 1\n')
        with patch.object(quality, '_tool_exists', return_value=False):
            result = quality.run_quality_checks([file], self.root)
        self.assertFalse(result['ok'])
        self.assertIn('ruff', [r['tool'] for r in result['results'] if not r['ok']])

    def test_project_local_binary_preferred(self):
        binary = self.root/'node_modules/.bin/eslint'
        binary.parent.mkdir(parents=True)
        binary.write_text('test')
        binary.chmod(0o700)
        self.assertEqual(quality.project_tool('eslint', self.root), str(binary))

    def test_quality_formats_only_requested_files_without_changing_behavior(self):
        file = self.root / 'format_me.py'
        file.write_text('import json, math\nx=1; y=2\npayload=json.dumps([x,y,math.floor(3.5)])\n')
        neighbor = self.root / 'leave_me.py'
        neighbor.write_text('a=1; b=2\n')
        original = file.read_text()
        before = {}
        exec(compile(original, str(file), 'exec'), before)
        result = quality.run_quality_checks([file], self.root, autofix=True)
        self.assertTrue(result['ok'], result)
        self.assertNotEqual(file.read_text(), original)
        after = {}
        exec(compile(file.read_text(), str(file), 'exec'), after)
        self.assertEqual(after['payload'], before['payload'])
        self.assertEqual(neighbor.read_text(), 'a=1; b=2\n')
        formatted = file.read_bytes()
        self.assertTrue(quality.run_quality_checks([file], self.root, autofix=True)['ok'])
        self.assertEqual(file.read_bytes(), formatted)

    def test_quality_default_is_read_only_even_with_project_fix_settings(self):
        (self.root / 'ruff.toml').write_text('fix = true\nfix-only = true\nunsafe-fixes = true\n')
        file = self.root / 'unchanged.py'
        source = 'import os\nx=1; y=2\n'
        file.write_text(source)
        self.assertFalse(quality.run_quality_checks([file], self.root)['ok'])
        self.assertEqual(file.read_text(), source)

    def test_quality_keeps_errors_and_unsafe_fixes_blocking(self):
        # Project settings must not widen the hook's automatic repair scope.
        (self.root / 'ruff.toml').write_text(
            'fix = true\nfix-only = true\nunsafe-fixes = true\n'
            '[lint]\nextend-safe-fixes = ["E731", "F401"]\n'
        )
        file = self.root / 'broken.py'
        for source, expected in (
            ('result = missing_name\n', 'F821'),
            ('import os\n', 'F401'),
            ('f = lambda x: x + 1\n', 'E731'),
            ('x = 1\nimport os\n\nprint(os.name, x)\n', 'E402'),
            ('def broken(:\n', 'SyntaxError'),
        ):
            with self.subTest(expected=expected):
                file.write_text(source)
                result = quality.run_quality_checks([file], self.root, autofix=True)
                self.assertFalse(result['ok'], result)
                self.assertIn(expected, '\n'.join(r['output'] for r in result['results']))
                self.assertEqual(file.read_text(), source)

    def test_quality_preserves_failing_tests_after_formatting(self):
        file = self.root / 'test_failure.py'
        file.write_text('def test_failure():\n    x=1; y=2\n    assert x == y\n')
        result = quality.run_quality_checks([file], self.root, autofix=True)
        self.assertFalse(result['ok'])
        self.assertTrue(any(r['tool'] == 'pytest' and not r['ok'] for r in result['results']))

    def test_quality_format_failure_and_deadline_cannot_pass(self):
        file = self.root / 'sample.py'
        file.write_text('x=1; y=2\n')
        binary = self.root / '.venv/bin/ruff'
        binary.parent.mkdir(parents=True)
        binary.write_text(f'#!{sys.executable}\nimport sys\n'
                          'sys.exit(2 if "format" in sys.argv else 0)\n')
        binary.chmod(0o700)
        result = quality.run_quality_checks([file], self.root, autofix=True)
        self.assertFalse(result['ok'])
        self.assertTrue(any(r['tool'] == 'ruff format' and not r['ok'] for r in result['results']))
        self.assertFalse(quality.run_quality_checks([file], self.root, timeout=0, autofix=True)['ok'])
        self.assertEqual(file.read_text(), 'x=1; y=2\n')

    def test_mypy_requires_actual_configuration(self):
        project = self.root/'pyproject.toml'
        project.write_text('[project]\nname="test"\n')
        self.assertFalse(quality.has_mypy_config(self.root))
        project.write_text('[tool.mypy]\nstrict=true\n')
        self.assertTrue(quality.has_mypy_config(self.root))

    def test_debugging_policy_reaches_both_injection_and_review(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('session_start.py', 'user_prompt_submit.py', 'mimo_audit.py'):
            text = (root/name).read_text()
            for required in ('.quality-gate.json', 'Hypothesis', 'ast-grep', 'py-spy', 'Tree-sitter', 'Repomix'):
                self.assertIn(required, text, (name, required))

    def test_native_stop_entrypoint_blocks_bad_contract_and_allows_good(self):
        import time
        home = self.root/'home'
        home.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        for agent in ('.codex', '.claude'):
            state = home/agent/'state'
            state.mkdir(parents=True)
            (state/'mimo_last_verdict.json').write_text(json.dumps({
                'gate-test': {'turn_id': 't1', 'verdict': 'PASS', 'updated_at': time.time()}
            }))
        env = dict(os.environ, HOME=str(home))
        payload = json.dumps({'session_id':'gate-test', 'turn_id':'t1', 'cwd':str(self.root),
                              'last_assistant_message':'Checked the project.'})
        self.contract([[sys.executable, '-c', 'raise SystemExit(1)']])
        read_only = subprocess.run([sys.executable, stop_orchestrator.__file__], input=payload,
                                   capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(read_only.stdout, '')
        (home/'.codex/state/pending_quality.json').write_text(json.dumps({
            'gate-test': [str(self.root/'deleted-repo')]
        }))
        deleted = subprocess.run([sys.executable, stop_orchestrator.__file__], input=payload,
                                 capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(deleted.stdout, '')
        self.assertEqual(json.loads((home/'.codex/state/pending_quality.json').read_text()), {})
        (home/'.codex/state/pending_quality.json').write_text(json.dumps({'gate-test': [str(self.root)]}))
        edited = subprocess.run([sys.executable, stop_orchestrator.__file__], input=payload,
                                capture_output=True, text=True, env=env, timeout=10)
        self.assertIn('Repository Quality Gate', edited.stdout)
        pause_transcript = self.root/'pause.jsonl'
        pause_transcript.write_text('\n'.join([
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                        'content': [{'type': 'text', 'text': 'ok standby for now'}]}}),
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                        'content': [{'type': 'text', 'text': '<environment_context>\n</environment_context>'}]}}),
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'Standby, no work started.'}]}}),
        ]))
        pause_payload = json.dumps({'session_id': 'gate-test', 'turn_id': 't2', 'cwd': str(self.root),
                                    'transcript_path': str(pause_transcript),
                                    'last_assistant_message': 'Standby, no work started.'})
        paused = subprocess.run([sys.executable, stop_orchestrator.__file__], input=pause_payload,
                                capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(paused.stdout, '')
        self.assertEqual(json.loads((home/'.codex/state/pending_quality.json').read_text())['gate-test'],
                         [str(self.root)])
        with pause_transcript.open('a') as stream:
            stream.write('\n' + json.dumps({'type': 'response_item', 'payload': {
                'type': 'function_call', 'name': 'exec_command', 'arguments': '{"cmd":"touch changed.py"}'}}))
        active = subprocess.run([sys.executable, stop_orchestrator.__file__], input=pause_payload,
                                capture_output=True, text=True, env=env, timeout=10)
        self.assertIn('Repository Quality Gate', active.stdout)
        (home/'.codex/state/pending_quality.json').unlink()
        payload = json.dumps({'session_id':'gate-test', 'turn_id':'t1', 'cwd':str(self.root),
                              'last_assistant_message':'Ready to deploy now.'})
        for code in (1, 0):
            (self.root / '.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']],
                'completion_commands': [[sys.executable, '-c', f'raise SystemExit({code})']],
            }))
            result = subprocess.run([sys.executable, stop_orchestrator.__file__], input=payload,
                                    capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            if code:
                self.assertEqual(json.loads(result.stdout)['decision'], 'block')
                self.assertIn('Repository Quality Gate', result.stdout)
            else:
                self.assertEqual(result.stdout, '')

    def test_gate_runs_without_milestone_or_dirty_tree(self):
        self.contract([[sys.executable, '-c', 'print("checked")']])
        with patch.object(stop_orchestrator, 'log'), patch.object(stop_orchestrator, 'milestone_detected', return_value=False):
            self.assertTrue(stop_orchestrator.run_aaa_gate(self.root, 'plain confirmation'))
        self.contract([[sys.executable, '-c', 'raise SystemExit(1)']])
        with patch.object(stop_orchestrator, 'block') as block, patch.object(stop_orchestrator, 'log'):
            self.assertFalse(stop_orchestrator.run_aaa_gate(self.root, 'done'))
            block.assert_called_once()

    def test_pending_survives_auto_commit_and_covers_multiple_repos(self):
        with patch.object(quality, 'PENDING_PATH', self.root/'state.json'):
            quality.pending_quality('session', self.root/'one')
            quality.pending_quality('session', self.root/'two')
            self.assertEqual(len(quality.pending_quality('session')), 2)
            quality.pending_quality('session', clear=True)
            self.assertEqual(quality.pending_quality('session'), [])

    def test_read_only_dirt_does_not_register_or_checkpoint(self):
        import io
        payload = {'tool_name': 'exec_command', 'cwd': str(self.root),
                   'tool_input': {'cmd': 'git status --short', 'workdir': str(self.root/'project')}}
        with patch.object(post_tool.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(post_tool, 'resolve_git_cwd', side_effect=lambda cwd, files: cwd), \
             patch.object(post_tool, 'remember_repo'), patch.object(post_tool, 'log'), \
             patch.object(post_tool, 'is_git_repo', return_value=True), \
             patch.object(post_tool, 'changed_files', return_value=[self.root/'old.py']), \
             patch.object(post_tool, 'run_quality_checks', return_value={'ok': True}) as check, \
             patch.object(post_tool, 'pending_quality') as pending, \
             patch.object(post_tool, 'sync_codegraph') as sync, \
             patch.object(post_tool, 'maybe_checkpoint', create=True) as checkpoint:
            post_tool.main()
            pending.assert_not_called()
            sync.assert_not_called()
            checkpoint.assert_not_called()
            check.assert_not_called()

    def test_multi_repo_edits_register_every_repo(self):
        import io
        files = [self.root/'one'/'app.py', self.root/'two'/'app.py']
        for file in files:
            file.parent.mkdir()
            file.write_text('x=1; y=2\n')
        payload = {'tool_name': 'Edit', 'cwd': str(self.root), 'tool_input': {}}
        with patch.object(post_tool.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(post_tool, 'changed_files_in_payload', return_value=files), \
             patch.object(post_tool, 'git_root', side_effect=lambda path: path), \
             patch.object(post_tool, 'resolve_git_cwd', return_value=self.root), \
             patch.object(post_tool, 'remember_repo'), patch.object(post_tool, 'log'), \
             patch.object(post_tool, 'run_quality_checks', wraps=quality.run_quality_checks) as check, \
             patch.object(post_tool, 'pending_quality') as pending, \
             patch.object(post_tool, 'sync_codegraph', side_effect=lambda root: \
                          self.assertNotIn(';', (root/'app.py').read_text())) as sync, \
             patch.object(post_tool, 'update_audit_state'), \
             patch.object(post_tool, 'maybe_mimo_checkpoint_audit'), patch.object(post_tool, 'maybe_checkpoint', create=True):
            post_tool.main()
            self.assertEqual({call.args[1] for call in pending.call_args_list}, {file.parent for file in files})
            self.assertEqual({call.args[0] for call in sync.call_args_list}, {file.parent for file in files})
            self.assertTrue(all(call.kwargs['autofix'] for call in check.call_args_list))

    def test_codegraph_initializes_then_syncs_existing_index(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        with patch.object(post_tool.subprocess, 'run') as run:
            run.return_value.returncode = 0
            post_tool.sync_codegraph(self.root)
            run.assert_called_once_with(['codegraph', 'init'], cwd=self.root,
                                        capture_output=True, text=True, timeout=20)
            run.reset_mock()
            (self.root/'.codegraph').mkdir()
            (self.root/'.codegraph/codegraph.db').write_text('')
            post_tool.sync_codegraph(self.root)
            run.assert_called_once_with(['codegraph', 'sync', '-q'], cwd=self.root,
                                        capture_output=True, text=True, timeout=10)
            run.return_value.returncode = 1
            run.return_value.stderr = 'index unavailable'
            with patch.object(post_tool, 'log') as log:
                post_tool.sync_codegraph(self.root)
                self.assertIn('index unavailable', log.call_args.args[1])


if __name__ == '__main__':
    unittest.main()

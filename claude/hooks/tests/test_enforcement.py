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
        self.root = Path(self.tmp.name)

    def contract(self, commands):
        (self.root / '.quality-gate.json').write_text(json.dumps({'commands': commands}))

    def test_contract_success_and_real_failure(self):
        for code in (0, 1):
            self.contract([[sys.executable, '-c', f'raise SystemExit({code})']])
            self.assertEqual(quality.run_project_gate(self.root)['ok'], code == 0)

    def test_missing_invalid_empty_and_missing_executable_block(self):
        self.assertFalse(quality.run_project_gate(self.root)['ok'])
        for commands in ([], 'echo pass', [[]], [['definitely-missing-quality-tool']]):
            self.contract(commands)
            self.assertFalse(quality.run_project_gate(self.root)['ok'])

    def test_deadline_blocks(self):
        self.contract([[sys.executable, '-c', 'import time; time.sleep(5)']])
        self.assertFalse(quality.run_project_gate(self.root, timeout=0.05)['ok'])

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
        env = dict(os.environ, HOME=str(home), CODEX_HOOK_AUTO_PR='0', CODEX_HOOK_CLAUDE_AUDIT='0')
        payload = json.dumps({'session_id':'gate-test', 'turn_id':'t1', 'cwd':str(self.root),
                              'last_assistant_message':'Checked the project.'})
        for code in (1, 0):
            self.contract([[sys.executable, '-c', f'raise SystemExit({code})']])
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
             patch.object(post_tool, 'maybe_checkpoint') as checkpoint:
            post_tool.main()
            pending.assert_not_called()
            checkpoint.assert_not_called()
            self.assertEqual(check.call_args.args[1], (self.root/'project').resolve())

    def test_multi_repo_edits_register_every_repo(self):
        import io
        files = [self.root/'one'/'app.py', self.root/'two'/'app.py']
        payload = {'tool_name': 'Edit', 'cwd': str(self.root), 'tool_input': {}}
        with patch.object(post_tool.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(post_tool, 'changed_files_in_payload', return_value=files), \
             patch.object(post_tool, 'git_root', side_effect=lambda path: path), \
             patch.object(post_tool, 'resolve_git_cwd', return_value=self.root), \
             patch.object(post_tool, 'remember_repo'), patch.object(post_tool, 'log'), \
             patch.object(post_tool, 'run_quality_checks', return_value={'ok': True}), \
             patch.object(post_tool, 'pending_quality') as pending, \
             patch.object(post_tool, 'update_checkpoint_state'), patch.object(post_tool, 'update_audit_state'), \
             patch.object(post_tool, 'maybe_mimo_checkpoint_audit'), patch.object(post_tool, 'maybe_checkpoint'):
            post_tool.main()
            self.assertEqual({call.args[1] for call in pending.call_args_list}, {file.parent for file in files})


if __name__ == '__main__':
    unittest.main()
